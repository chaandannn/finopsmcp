# SPDX-License-Identifier: Apache-2.0
"""attribution MCP tools (extracted from server.py; see finops/tools/__init__.py).

Server-local helpers, globals, and the mcp instance are reached through the live
server module (_srv.NAME) so monkeypatching finops.server.* still works and no
import-order coupling exists."""
from __future__ import annotations

from .. import server as _srv


@_srv.mcp.tool()
async def get_costs_by_team(
    start_date: str | None = None,
    end_date: str | None = None,
    provider: str | None = None,
) -> dict:
    """
    Return cloud costs broken down by engineering team, using tag attribution rules.

    Requires:
    - Tag rules configured in ~/.finops/tag_rules.yaml (run 'uvx nable' → tags)
    - Cloud providers that support tag-based cost grouping (AWS, Azure, GCP)

    Args:
        start_date: ISO date. Defaults to 30 days ago.
        end_date: ISO date. Defaults to today.
        provider: Filter to a specific provider.

    Examples:
        - "How much is the data team spending?"
        - "Show me cloud costs by team this month"
        - "Which team has the highest AWS bill?"
    """

    from ..storage.snapshots import get_costs_by_team as _get

    sd, ed = _srv._default_dates()
    if start_date:
        sd = _srv.date.fromisoformat(start_date)
    if end_date:
        ed = _srv.date.fromisoformat(end_date)

    rows = _get(sd, ed, provider=provider)
    if not rows:
        return {
            "data": [],
            "message": (
                "No attributed cost data found. "
                "Ensure tag_rules.yaml is configured and run 'take_snapshot_now' to populate data."
            ),
            "diagnostic": _attribution_diagnostic(),
        }

    by_team: dict[str, float] = {}
    for r in rows:
        team = r["team"] or "unattributed"
        by_team[team] = by_team.get(team, 0.0) + float(r["total_usd"])

    grand = sum(by_team.values())
    ranked = sorted(
        [{"team": t, "total_usd": round(v, 4), "total_formatted": _srv._fmt_usd(v), "pct": round(v / grand * 100, 1) if grand else 0}
         for t, v in by_team.items()],
        key=lambda x: -x["total_usd"],
    )

    out = {
        "period": {"start": sd.isoformat(), "end": ed.isoformat()},
        "grand_total_usd": round(grand, 4),
        "grand_total_formatted": _srv._fmt_usd(grand),
        "by_team": ranked,
    }

    # Everything in one bucket named "unattributed" is not a report, it is a
    # symptom, and the most common cause is a tag key that was never activated
    # for cost allocation. Say so here rather than letting the user conclude
    # their tagging is broken when it is not.
    if len(by_team) == 1 and "unattributed" in by_team:
        note = _attribution_diagnostic()
        if note:
            out["diagnostic"] = note
    return out


def _attribution_diagnostic() -> str | None:
    """Best-effort explanation for empty attribution. Never raises, never guesses."""
    try:
        from ..attribution.activation import explain_empty_attribution
        from ..attribution.mapper import configured_tag_keys

        return explain_empty_attribution(configured_tag_keys())
    except Exception:  # noqa: BLE001 - a diagnostic must never break the answer
        return None


@_srv.mcp.tool()
async def run_attribution_now(
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """
    Fetch tagged cost data from AWS/Azure/GCP and store team attributions.
    Run this after setting up tag_rules.yaml to populate team cost data.

    Args:
        start_date: ISO date. Defaults to 30 days ago.
        end_date: ISO date. Defaults to today.

    Examples:
        - "Run tag attribution now"
        - "Update team cost data"
    """

    from ..attribution.fetcher import fetch_aws_tagged_costs
    from ..attribution.mapper import _load_rules
    from ..storage.snapshots import store_attributed_cost

    sd, ed = _srv._default_dates()
    if start_date:
        sd = _srv.date.fromisoformat(start_date)
    if end_date:
        ed = _srv.date.fromisoformat(end_date)

    cfg = _load_rules()
    tag_keys = list({r.get("tag_key", "") for r in cfg.get("rules", []) if r.get("tag_key")})

    total_stored = 0
    errors: dict[str, str] = {}

    if _srv.os.environ.get("AWS_ACCESS_KEY_ID") or _srv.os.environ.get("AWS_ROLE_ARNS"):
        try:
            role_arns = [a.strip() for a in _srv.os.environ.get("AWS_ROLE_ARNS", "").split(",") if a.strip()]
            rows = fetch_aws_tagged_costs(sd, ed, tag_keys, role_arns or None)
            for row in rows:
                attr = row["attribution"]
                store_attributed_cost(
                    provider="aws",
                    service=row["service"],
                    account_id=row["account_id"],
                    team=attr.get("team", "unattributed"),
                    environment=attr.get("environment", ""),
                    snapshot_date=sd,
                    amount_usd=row["amount_usd"],
                )
                total_stored += 1
        except Exception as e:
            errors["aws"] = str(e)

    return {
        "status": "complete",
        "records_stored": total_stored,
        "errors": errors,
        "period": {"start": sd.isoformat(), "end": ed.isoformat()},
        "tip": "If data is empty, check that ~/.finops/tag_rules.yaml is configured with your tag keys.",
    }


@_srv.mcp.tool()
async def get_efficiency_scorecard(
    scope: str = "overall",
    team: str | None = None,
    environment: str | None = None,
    provider: str | None = None,
) -> dict:
    """
    FinOps efficiency scorecard, a 0–100 score with letter grade across
    5 dimensions: compute efficiency, waste reduction, commitment coverage,
    tag hygiene, and anomaly response. Tracked over time so you can see
    if you're improving.

    Scope options:
      - "overall"        , everything combined (default)
      - team=platform    , filter by team tag
      - environment=prod , filter by environment tag
      - provider=aws     , single provider view

    Examples:
        - "What's our FinOps score?"
        - "Show me the efficiency scorecard for the platform team"
        - "How is our AWS efficiency rated?"
        - "What's our worst performing dimension?"
        - "Are we improving or getting worse on cloud efficiency?"
    Args:
        scope: "org" (default) or "team" for a single team's scorecard.
        team: Team name from your attribution tags, when scope="team".
        environment: Limit to one environment (e.g. "prod").
        provider: Limit to one provider (e.g. "aws"). None = all.

    """
    from ..scoring.scorecard import build_scorecard

    # Build scope identifier and label
    if team:
        scope = f"team:{team}"
        label = f"{team.title()} team"
    elif environment:
        scope = f"env:{environment}"
        label = f"{environment.title()} environment"
    elif provider:
        scope = f"provider:{provider}"
        label = f"{provider.upper()}"
    else:
        scope = "overall"
        label = "Overall"

    try:
        # Gather available data for scoring
        k8s_reports = None
        idle_res     = None
        commitment   = None

        # Try Kubernetes
        try:
            from ..connectors.kubernetes import KubernetesConnector
            conn = KubernetesConnector()
            if await conn.is_configured():
                k8s_reports = conn.analyze_all_clusters()
        except Exception:
            pass

        # Try idle resources from DB
        try:
            from ..storage.db import get_engine, resource_inventory
            from sqlalchemy import select
            with get_engine().connect() as db:
                rows = db.execute(
                    select(resource_inventory).where(
                        resource_inventory.c.is_active == True,
                        resource_inventory.c.monthly_cost_usd == 0.0,
                    ).limit(100)
                ).fetchall()
                idle_res = [dict(r._mapping) for r in rows] if rows else None
        except Exception:
            pass

        # Try commitment data, scoped by tag when filtering by team/env
        tag_filter: dict | None = None
        if team:
            tag_filter = {"team": team}
        elif environment:
            tag_filter = {"env": environment}

        try:
            from ..recommendations.commitments import analyze_commitments
            raw_commits = analyze_commitments(tag_filter=tag_filter)
            if raw_commits:
                commitment = {
                    # None-safe: either instrument may be unreadable. Falls back
                    # to 0.0 only for the scorecard's numeric contract, and
                    # coverage_known says which it was.
                    "coverage_pct": raw_commits.combined_coverage_pct or 0.0,
                    "coverage_known": raw_commits.combined_coverage_pct is not None,
                    "on_demand_usd": raw_commits.uncovered_on_demand_usd,
                    "potential_savings_usd": sum(
                        r.get("monthly_savings", 0)
                        for r in raw_commits.recommendations
                        if r.get("type") != "warning"
                    ),
                }
        except Exception:
            pass

        # Get total spend from DB snapshots
        total_spend = 0.0
        try:
            from ..storage.db import cost_snapshots, get_engine
            from sqlalchemy import select, func
            cutoff = (_srv.date.today() - _srv.timedelta(days=30)).isoformat()
            with get_engine().connect() as db:
                row = db.execute(
                    select(func.sum(cost_snapshots.c.amount_usd)).where(
                        cost_snapshots.c.snapshot_date >= cutoff
                    )
                ).scalar()
                total_spend = float(row or 0)
        except Exception:
            pass

        # Try tag coverage from attributed vs total costs
        untagged_spend = 0.0
        try:
            from ..storage.db import attributed_costs, cost_snapshots, get_engine
            from sqlalchemy import select, func
            cutoff = (_srv.date.today() - _srv.timedelta(days=30)).isoformat()
            with get_engine().connect() as db:
                attributed = db.execute(
                    select(func.sum(attributed_costs.c.amount_usd)).where(
                        attributed_costs.c.snapshot_date >= cutoff,
                        attributed_costs.c.team != "unattributed",
                    )
                ).scalar() or 0
                untagged_spend = max(0.0, total_spend - float(attributed))
        except Exception:
            pass

        scorecard = build_scorecard(
            scope=scope,
            label=label,
            k8s_reports=k8s_reports,
            idle_resources=idle_res,
            commitment_data=commitment,
            untagged_spend_usd=untagged_spend,
            total_monthly_spend=total_spend,
            tag_filter=tag_filter,
        )

        return scorecard.as_dict()

    except Exception as e:
        _srv.log.exception("Scorecard generation failed")
        return {"error": str(e)}


@_srv.mcp.tool()
async def get_team_scorecards() -> dict:
    """
    Efficiency scorecard for every team, side by side.
    Teams are discovered from your cost attribution tags (team=X).
    Shows which teams are leading and which need help.

    Examples:
        - "Show me efficiency scores for all teams"
        - "Which team has the worst FinOps score?"
        - "Compare cloud efficiency across teams"
        - "Who is leading on waste reduction?"
    """
    from ..scoring.scorecard import build_scorecard
    from datetime import timedelta

    try:
        # Discover teams from attribution data
        teams: list[str] = []
        try:
            from ..storage.db import attributed_costs, get_engine
            from sqlalchemy import select, distinct
            cutoff = (_srv.date.today() - timedelta(days=30)).isoformat()
            with get_engine().connect() as db:
                rows = db.execute(
                    select(distinct(attributed_costs.c.team)).where(
                        attributed_costs.c.snapshot_date >= cutoff,
                        attributed_costs.c.team != "unattributed",
                        attributed_costs.c.team != "",
                    )
                ).fetchall()
                teams = [r[0] for r in rows]
        except Exception:
            pass

        if not teams:
            return {
                "error": "No team attribution data found. "
                         "Run `run_attribution_now` first to tag spend by team, "
                         "or ensure resources have a 'team' tag."
            }

        scorecards = []
        for team in teams[:10]:  # cap at 10 teams
            sc = build_scorecard(scope=f"team:{team}", label=f"{team} team")
            scorecards.append({
                "team": team,
                "score": sc.total_score,
                "grade": sc.grade,
                "trend": sc.trend,
                "trend_delta": sc.trend_delta,
                "potential_savings_usd": sc.potential_savings_usd,
                "dimensions": {d.name: round(d.raw_score, 1) for d in sc.dimensions},
                "top_win": sc.top_wins[0] if sc.top_wins else None,
            })

        scorecards.sort(key=lambda s: s["score"])

        leader    = max(scorecards, key=lambda s: s["score"])
        laggard   = min(scorecards, key=lambda s: s["score"])
        avg_score = _srv.statistics.mean(s["score"] for s in scorecards)

        return {
            "team_count": len(scorecards),
            "average_score": round(avg_score, 1),
            "leader": leader["team"],
            "needs_most_help": laggard["team"],
            "teams": scorecards,
            "summary": (
                f"{len(scorecards)} teams scored. "
                f"Avg: {avg_score:.0f}/100. "
                f"Leader: {leader['team']} ({leader['grade']}, {leader['score']:.0f}pts). "
                f"Most opportunity: {laggard['team']} ({laggard['grade']}, {laggard['score']:.0f}pts)."
            ),
        }

    except Exception as e:
        _srv.log.exception("Team scorecards failed")
        return {"error": str(e)}


@_srv.mcp.tool()
async def get_label_costs(
    label_key: str = "team",
    context: str | None = None,
) -> dict:
    """
    Aggregate Kubernetes costs by any pod label across all namespaces.
    Great for chargeback: see spend by team, environment, app, or any label.

    Workloads without the label are grouped under '__untagged__'. If tagging
    coverage is low, the response includes a warning with the tagged %.

    Common label_key values: team, env, environment, app, component, tier,
    app.kubernetes.io/name, app.kubernetes.io/part-of

    Examples:
        - "Show me Kubernetes costs by team"
        - "Which team is spending the most on Kubernetes?"
        - "Break down K8s costs by environment"
        - "How much is the payments team spending in the cluster?"
        - "Show K8s cost by app label"
        - "What percentage of our cluster is untagged?"
    Args:
        label_key: Kubernetes label key to group costs by (e.g. "app", "team").
        context: Kubernetes context name from list_kubernetes_contexts(). Default context when omitted.

    """
    try:
        from ..connectors.kubernetes import KubernetesConnector
    except ImportError:
        return {"error": "kubernetes package not installed. Run: pip install finops-mcp[kubernetes]"}

    try:
        connector = KubernetesConnector()
        if not await connector.is_configured():
            return {"error": "No kubeconfig found. Set KUBECONFIG or ensure ~/.kube/config exists."}

        report = connector.analyze_cluster(context)
        result = connector.get_label_costs(report, label_key=label_key)

        # Human-readable summary
        rows = result.get("by_label", [])
        top3 = rows[:3]
        top_str = ", ".join(f"{r['label_value']}: ${r['monthly_cost_usd']:,.0f}" for r in top3)
        tagged_pct = result.get("tagged_workload_pct", 0)
        result["summary"] = (
            f"Cluster '{report.cluster}' by {label_key}: {top_str}. "
            f"{tagged_pct}% of cost tagged."
        )

        return result
    except Exception as e:
        _srv.log.exception("Label cost breakdown failed")
        return {"error": str(e)}


@_srv.mcp.tool()
async def list_org_accounts() -> dict:
    """
    List all AWS Organization member accounts, discovering them via the
    AWS Organizations API. Syncs account metadata to local DB for future queries.
    Account listing is free. Detailed cost rollup across accounts requires a Pro plan.

    Requires: AWS credentials with organizations:ListAccounts permission
    (management account or delegated admin).

    Examples:
        - "List all accounts in the AWS org"
        - "Show me all AWS member accounts"
        - "How many AWS accounts do we have?"
    """
    try:
        from ..connectors.aws_org import list_org_accounts
        accounts = list_org_accounts(sync_to_db=True)
        if not accounts:
            return {
                "message": "No accounts found. Ensure AWS credentials have organizations:ListAccounts permission.",
                "accounts": [],
            }
        mgmt = [a for a in accounts if a.get("is_management_account")]
        members = [a for a in accounts if not a.get("is_management_account")]
        members.sort(key=lambda a: (a.get("account_name") or "").lower())
        kept, omitted = _srv.fit_to_budget(members, max_tokens=6000)
        result = {
            "total_accounts": len(accounts),
            "member_account_count": len(members),
            "management_account": mgmt[0] if mgmt else None,
            "member_accounts": kept,
        }
        if omitted > 0:
            result["member_accounts_truncated"] = omitted
            result["hint"] = (
                f"Showing {len(kept)} of {len(members)} member accounts (sorted by name); "
                f"{omitted} omitted to bound context. All {len(accounts)} accounts were "
                "synced to the local DB and are queryable by account_id."
            )
        return result
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def get_org_cost_summary(days_back: int = 30) -> dict:
    """
    Get a cost rollup across all AWS Organization accounts: total spend,
    per-account breakdown sorted by spend, and top services per account.
    Requires a Pro plan (org_reports).

    Args:
        days_back: Look-back period in days (default 30)

    Examples:
        - "Show me org-wide cloud costs"
        - "Which account is spending the most?"
        - "Give me a breakdown of costs across all accounts"
        - "What's our total AWS spend across the whole org?"
    """
    if err := _srv.require_pro("org_reports"):
        return err
    try:
        from ..connectors.aws_org import org_cost_summary
        result = org_cost_summary(days_back=days_back)
        accounts = result.get("accounts") if isinstance(result, dict) else None
        if accounts:
            # accounts is pre-sorted by total_usd desc; cap detail, keep aggregates.
            kept, omitted = _srv.fit_to_budget(accounts, max_tokens=6000)
            result["accounts"] = kept
            if omitted > 0:
                result["accounts_truncated"] = omitted
                result["hint"] = (
                    f"showing top {len(kept)} of {result.get('account_count', len(accounts))} "
                    f"accounts by spend; org_total_usd reflects all accounts. "
                    f"Use get_top_spending_accounts or filter by account for more detail."
                )
        return result
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def get_ou_cost_breakdown(days_back: int = 30) -> dict:
    """
    Break costs down by AWS Organizational Unit (OU). When OUs map to
    departments or teams, this gives you a clean chargeback report.
    Requires a Pro plan (org_reports).

    Args:
        days_back: Look-back period in days (default 30)

    Examples:
        - "Break down costs by business unit"
        - "Show OU-level cost breakdown"
        - "How much is each department spending in AWS?"
    """
    if err := _srv.require_pro("org_reports"):
        return err
    try:
        from ..connectors.aws_org import ou_cost_breakdown
        breakdown = ou_cost_breakdown(days_back=days_back)
        return {"ous": breakdown, "days_back": days_back}
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def get_tag_cost_breakdown_cur(
    tag_key: str = "team",
    start_date: str | None = None,
    end_date: str | None = None,
    cost_type: str = "unblended",
) -> dict:
    """
    Break AWS costs down by a resource tag using CUR line-item data via Athena.

    Supports both unblended and amortized cost types. Resources missing the
    specified tag are grouped under "__untagged__". Pro plan feature.

    Args:
        tag_key: Tag key to group by (e.g. "team", "env", "project").
        start_date: ISO date (YYYY-MM-DD). Defaults to 30 days ago.
        end_date: ISO date. Defaults to today.
        cost_type: "unblended" (default) or "amortized" (applies effective
                   SP/RI rates instead of list price).

    Examples:
        - "Show me AWS costs broken down by team tag"
        - "What is each environment costing us in CUR?"
        - "Break down amortized costs by project tag"
    """

    sd, ed = _srv._default_dates()
    if start_date:
        sd = _srv.date.fromisoformat(start_date)
    if end_date:
        ed = _srv.date.fromisoformat(end_date)

    try:
        from ..connectors.cur import get_tag_cost_breakdown
        return get_tag_cost_breakdown(
            tag_key=tag_key,
            start_date=sd,
            end_date=ed,
            cost_type=cost_type,
        )
    except Exception as exc:
        _srv.log.error("get_tag_cost_breakdown_cur failed: %s", exc)
        return {"error": str(exc)}


@_srv.mcp.tool()
async def audit_terraform_tags(
    tf_dir: str,
    state_path: str | None = None,
) -> dict:
    """
    Scan Terraform state for resources missing required tags.
    Runs `terraform show -json` in tf_dir (or reads state_path directly).
    Required tags configured via FINOPS_REQUIRED_TAGS env var (comma-separated,
    default: team,environment,service).

    Args:
        tf_dir: Path to the Terraform working directory (must be initialized).
        state_path: Optional path to a .tfstate file. Skips terraform CLI if provided.

    Examples:
        - "Audit tags in our infra repo"
        - "Which resources are missing the team tag?"
    """
    if err := _srv.require_role("analyst"):
        return err

    safe_dir = _srv._resolve_safe_path(tf_dir, must_exist=True)
    if isinstance(safe_dir, dict):
        return safe_dir
    tf_dir = safe_dir

    if state_path is not None:
        safe_state = _srv._resolve_safe_path(state_path, must_exist=True)
        if isinstance(safe_state, dict):
            return safe_state
        state_path = safe_state

    from ..connectors.terraform import audit_tags, persist_violations, _required_tags

    try:
        violations = audit_tags(tf_dir, state_path)
    except Exception as exc:
        return {"error": str(exc), "tf_dir": tf_dir}

    stored = persist_violations(tf_dir, violations)

    kept, omitted = _srv.fit_to_budget(violations, max_tokens=6000)
    result = {
        "tf_dir": tf_dir,
        "required_tags": _required_tags(),
        "violations_found": len(violations),
        "stored_in_db": stored,
        "violations": kept,
    }
    if omitted:
        result["violations_truncated"] = omitted
        result["hint"] = f"{omitted} more violations omitted to save tokens; all {len(violations)} are stored in the DB."
    return result


@_srv.mcp.tool()
async def generate_terraform_tag_fixes(
    tf_dir: str,
) -> dict:
    """
    Generate HCL patches for all open tag violations in tf_dir.
    Shows a unified diff per .tf file, does NOT write to disk.
    Run audit_terraform_tags first to populate violations.

    Args:
        tf_dir: Same directory passed to audit_terraform_tags.

    Examples:
        - "Show me the tag fixes needed"
        - "What HCL changes are required to fix our tagging?"
    """
    # Drafting fixes is remediation (Pro), same gate as opening the PR itself.
    if (err := _srv.require_pro("remediation")):
        return err
    if err := _srv.require_role("analyst"):
        return err

    safe_dir = _srv._resolve_safe_path(tf_dir, must_exist=True)
    if isinstance(safe_dir, dict):
        return safe_dir
    tf_dir = safe_dir

    import json as _json
    from sqlalchemy import select
    from ..storage.db import terraform_tag_audits, get_engine
    from ..tagging.hcl_patcher import generate_all_fixes

    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(terraform_tag_audits).where(
                terraform_tag_audits.c.tf_dir == tf_dir,
                terraform_tag_audits.c.status == "open",
            )
        ).fetchall()

    if not rows:
        return {
            "message": "No open violations found. Run audit_terraform_tags first.",
            "diffs": {},
        }

    violations = [
        {
            "address": r.resource_address,
            "type": r.resource_type,
            "name": r.resource_name,
            "current_tags": _json.loads(r.current_tags),
            "missing_tags": _json.loads(r.missing_tags),
            "file_path": r.file_path or "",
        }
        for r in rows
    ]

    try:
        diffs = generate_all_fixes(tf_dir, violations)
    except Exception as exc:
        return {"error": str(exc)}

    total_files = len(diffs)
    # diffs is a dict keyed by .tf path with a full unified diff string each.
    # Cap the included diffs to the largest (most-changed) files within a token
    # budget. Never drop the counts so the model can still state the full picture.
    diff_items = sorted(diffs.items(), key=lambda kv: len(kv[1] or ""), reverse=True)
    kept_diffs: dict = {}
    used_tokens = 0
    budget = 6000
    for path, diff in diff_items:
        cost = _srv.estimate_tokens(diff) + _srv.estimate_tokens(path)
        if kept_diffs and used_tokens + cost > budget:
            break
        kept_diffs[path] = diff
        used_tokens += cost

    omitted = total_files - len(kept_diffs)
    result = {
        "violations_count": len(violations),
        "files_to_patch": total_files,
        "diffs": kept_diffs,
    }
    if omitted > 0:
        omitted_paths = [p for p, _ in diff_items[len(kept_diffs):]]
        result["diffs_truncated"] = omitted
        result["omitted_files"] = omitted_paths
        result["hint"] = (
            f"showing diffs for {len(kept_diffs)} of {total_files} files "
            f"(largest first) to save tokens; run open_terraform_tag_pr to apply "
            f"all fixes including the omitted files."
        )
    return result


@_srv.mcp.tool()
async def open_terraform_tag_pr(
    tf_dir: str,
    github_repo: str,
    branch: str = "fix/add-required-tags",
    base_branch: str = "main",
    pr_title: str = "fix: add required tags to Terraform resources",
) -> dict:
    """
    Apply tag fixes to .tf files and open a GitHub PR.
    Requires GITHUB_TOKEN env var and a git remote configured for github_repo.

    Args:
        tf_dir: Path to the Terraform working directory (must be a git repo).
        github_repo: GitHub repo in "owner/repo" format.
        branch: Branch name to create. Defaults to "fix/add-required-tags".
        base_branch: Target branch for the PR. Defaults to "main".
        pr_title: PR title.

    Examples:
        - "Open a PR to fix the tagging gaps"
        - "Create the tag fix PR against main"
    """
    if (err := _srv.require_pro("remediation")):
        return err
    if err := _srv.require_role("analyst"):
        return err

    from ..remediation.gate import remediation_pr_enabled, disabled_response
    if not remediation_pr_enabled():
        # This tool only opens a PR; there is no local-only mode to fall back to.
        return disabled_response(dry_run_hint=False)

    safe_dir = _srv._resolve_safe_path(tf_dir, must_exist=True)
    if isinstance(safe_dir, dict):
        return safe_dir
    tf_dir = safe_dir

    import json as _json
    import subprocess as _sp
    from sqlalchemy import select
    from ..storage.db import terraform_tag_audits, get_engine
    from ..tagging.hcl_patcher import apply_fixes
    from ..integrations.ticketing import create_github_pr

    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(terraform_tag_audits).where(
                terraform_tag_audits.c.tf_dir == tf_dir,
                terraform_tag_audits.c.status == "open",
            )
        ).fetchall()

    if not rows:
        return {
            "message": "No open violations. Run audit_terraform_tags first.",
            "pr_url": None,
        }

    violations = [
        {
            "address": r.resource_address,
            "type": r.resource_type,
            "name": r.resource_name,
            "current_tags": _json.loads(r.current_tags),
            "missing_tags": _json.loads(r.missing_tags),
            "file_path": r.file_path or "",
        }
        for r in rows
    ]

    # 1. Apply fixes to disk
    try:
        modified_files = apply_fixes(tf_dir, violations)
    except Exception as exc:
        return {"error": f"Failed to apply fixes: {exc}"}

    if not modified_files:
        return {
            "message": (
                "No .tf files were modified. Violations may not be locatable in source. "
                "Ensure tf_dir contains .tf files with matching resource declarations."
            ),
            "pr_url": None,
        }

    # 2. Git: checkout branch, stage, commit, push
    def run_git(*args: str) -> str:
        result = _sp.run(
            ["git", *args], cwd=tf_dir, capture_output=True, text=True
        )
        if result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    # Reject branch names git would parse as options (argument-injection -> RCE).
    _ref_ok = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/-")
    for _ref, _kind in ((branch, "branch"), (base_branch, "base_branch")):
        if (not _ref or _ref[0] == "-" or ".." in _ref or _ref.endswith((".lock", "/"))
                or len(_ref) > 200 or any(_c not in _ref_ok for _c in _ref)):
            return {"error": f"Unsafe {_kind} {_ref!r}: refs may use [A-Za-z0-9._/-] and must not start with '-'."}

    try:
        run_git("checkout", "-b", branch)
        run_git("add", "--", *modified_files)
        run_git(
            "commit", "-m",
            f"fix: add required tags to Terraform resources\n\n"
            f"Fixed {len(violations)} missing tag violations across "
            f"{len(modified_files)} file(s).\n\n"
            f"Co-Authored-By: nable FinOps MCP <noreply@nable.dev>",
        )
        run_git("push", "-u", "origin", branch)
    except Exception as exc:
        return {"error": f"Git operation failed: {exc}", "branch": branch}

    # 3. Open GitHub PR
    violation_lines = "\n".join(
        f"- `{v['address']}` - missing: {', '.join(v['missing_tags'])}"
        for v in violations[:30]
    )
    if len(violations) > 30:
        violation_lines += f"\n\n_...and {len(violations) - 30} more_"

    pr_body = (
        f"## Summary\n\n"
        f"Adds missing required tags to {len(violations)} Terraform resource(s) "
        f"across {len(modified_files)} file(s).\n\n"
        f"### Resources fixed\n\n"
        f"{violation_lines}\n\n"
        f"---\n"
        f"🤖 Generated by [nable FinOps MCP](https://github.com/nable-finops/nable)"
    )

    try:
        pr_resp = create_github_pr(
            repo=github_repo,
            title=pr_title,
            body=pr_body,
            head=branch,
            base=base_branch,
        )
        pr_url = pr_resp.get("html_url", "")
    except Exception as exc:
        return {"error": f"PR creation failed: {exc}", "branch": branch}

    # 4. Mark violations as fixed in DB
    ids = [r.id for r in rows]
    with engine.begin() as conn:
        conn.execute(
            terraform_tag_audits.update()
            .where(terraform_tag_audits.c.id.in_(ids))
            .values(status="fixed", pr_url=pr_url)
        )

    return {
        "pr_url": pr_url,
        "branch": branch,
        "violations_fixed": len(violations),
        "files_modified": modified_files,
    }


@_srv.mcp.tool()
async def get_agent_team() -> dict:
    """
    The nable agent team: Budget Guard, Savings Analyst, and the Ledger, with
    each agent's status on this install and the one step that finishes its setup.

    Budget Guard gates agent actions (cost + budget + policy + your approval
    history). Savings Analyst judges genuine savings on your real rates and
    drafts the fix as a PR. The Ledger records decisions, verifies savings
    landed, and teaches the other two. All propose-only.

    Call this when the user asks about nable's agents, "set up the agent team",
    "is the budget guard on?", "why isn't nable learning?", or after activating
    a license, so they see what just unlocked and what to do next.

    Examples:
        - "Show me the agent team"
        - "Set up nable's agents"
        - "Is the budget guard active?"
    """
    from ..agent_controls import agent_team_status
    return agent_team_status()
