# SPDX-License-Identifier: Apache-2.0
"""kubernetes MCP tools (extracted from server.py; see finops/tools/__init__.py).

Server-local helpers, globals, and the mcp instance are reached through the live
server module (_srv.NAME) so monkeypatching finops.server.* still works and no
import-order coupling exists."""
from __future__ import annotations

from .. import server as _srv


@_srv.mcp.tool()
async def create_kubernetes_waste_tickets(
    min_monthly_waste: float = 50.0,
) -> dict:
    """
    Create tickets for Kubernetes waste findings: idle nodes, over-provisioned
    workloads, and orphaned Helm releases.

    Args:
        min_monthly_waste: Only ticket findings above this threshold (default $50/mo)

    Examples:
        - "Create tickets for all Kubernetes waste"
        - "File Jira issues for idle K8s nodes"
        - "Open issues for orphaned Helm releases"
    """
    if err := _srv.require_pro("ticket_creation"):
        return err

    try:
        from ..connectors.kubernetes import KubernetesConnector
        from ..connectors.helm import discover_helm_releases
        from ..integrations.ticketing import create_kubernetes_waste_ticket

        urls = []
        k8s_conn = KubernetesConnector()

        # Idle nodes and over-provisioned workloads
        # report is a ClusterReport dataclass; node_utilization is list[dict]
        reports = k8s_conn.analyze_all_clusters()
        for report in reports:
            # Idle nodes, idle_nodes is list[str] of node names
            for node in report.node_utilization:
                if node["node"] in report.idle_nodes and node["monthly_cost"] >= min_monthly_waste:
                    finding = {
                        "kind": "idle_node",
                        "cluster": report.cluster,
                        "name": node["node"],
                        "monthly_waste_usd": node["monthly_cost"],
                        "detail": (
                            f"CPU: {node.get('cpu_requested_pct', 0):.0f}%, "
                            f"Mem: {node.get('mem_requested_pct', 0):.0f}% utilized"
                        ),
                    }
                    url = create_kubernetes_waste_ticket(finding)
                    if url:
                        urls.append({"type": "idle_node", "name": node["node"], "url": url})

            # Over-provisioned workloads, rightsizing_opportunities is list[dict]
            for opp in report.rightsizing_opportunities:
                waste = opp.get("potential_savings_usd", 0)
                if waste >= min_monthly_waste:
                    finding = {
                        "kind": "over_requested",
                        "cluster": report.cluster,
                        "namespace": opp.get("namespace", ""),
                        "name": opp.get("workload", ""),
                        "monthly_waste_usd": waste,
                        "detail": "; ".join(opp.get("issues", [])),
                    }
                    url = create_kubernetes_waste_ticket(finding)
                    if url:
                        urls.append({"type": "over_provisioned", "name": opp.get("workload"), "url": url})

        # Orphaned Helm releases, discover_helm_releases requires a k8s client
        try:
            k8s_client = k8s_conn._load_client()
            releases = discover_helm_releases(k8s_client)
            for rel in releases:
                if rel.is_orphaned and rel.monthly_cost >= min_monthly_waste:
                    finding = {
                        "kind": "orphaned_helm",
                        "cluster": "default",
                        "namespace": rel.namespace,
                        "name": rel.name,
                        "monthly_waste_usd": rel.monthly_cost,
                        "detail": (
                            f"Chart: {rel.chart}, deployed "
                            f"{rel.deployed_at[:10] if rel.deployed_at else 'unknown'}, "
                            f"0 running pods"
                        ),
                    }
                    url = create_kubernetes_waste_ticket(finding)
                    if url:
                        urls.append({"type": "orphaned_helm", "name": rel.name, "url": url})
        except Exception:
            pass  # Helm optional

        return {
            "tickets_created": len(urls),
            "tickets": urls,
        }
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def list_kubernetes_contexts() -> dict:
    """
    List all Kubernetes contexts available in the local kubeconfig, and show
    which one is currently active. Use this to discover what to pass as the
    'context' argument to get_kubernetes_costs.

    Examples:
        - "What Kubernetes clusters do I have?"
        - "List my kubeconfig contexts"
        - "Which K8s context is currently active?"
    """
    try:
        from kubernetes import config as k8s_config  # type: ignore
    except ImportError:
        return {"error": "kubernetes package not installed. Run: pip install finops-mcp[kubernetes]"}

    try:
        current_ctx, all_contexts = k8s_config.list_kube_config_contexts()
        names = [c["name"] for c in (all_contexts or [])]
        current_name = (current_ctx or {}).get("name", "")
        return {
            "current_context": current_name,
            "available_contexts": names,
            "count": len(names),
        }
    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def get_kubernetes_costs(
    context: str | None = None,
    namespace: str | None = None,
) -> dict:
    """
    Full Kubernetes cost breakdown -- node costs attributed to namespaces,
    workloads, and labels. Detects wasted spend and rightsizing opportunities.

    Requires: pip install finops-mcp[kubernetes]
    Optional: metrics-server in-cluster for actual CPU/memory usage data.

    Examples:
        - "How much does our Kubernetes cluster cost?"
        - "Which namespace is spending the most?"
        - "Show me wasted Kubernetes spend"
        - "Which pods are over-provisioned?"
        - "What's our cluster CPU efficiency?"
    Args:
        context: Kubernetes context name from list_kubernetes_contexts(). Default context when omitted.
        namespace: Limit to one Kubernetes namespace. All namespaces when omitted.

    """
    try:
        from ..connectors.kubernetes import KubernetesConnector
    except ImportError:
        return {"error": "kubernetes package not installed. Run: pip install finops-mcp[kubernetes]"}

    from ..demo_data import is_demo, get_demo_response
    if is_demo():
        return get_demo_response("get_kubernetes_costs") or {}

    # Prefer OpenCost when it is configured and reachable: it prices GPU, network,
    # and PV storage at the cluster's real rates, which nable's built-in list-price
    # allocator does not. Fall back to the built-in estimate when OpenCost is
    # absent, so k8s cost still works with zero setup. Either way the result is
    # tagged is_estimate so the user knows which they are seeing.
    try:
        from ..connectors import opencost as _oc
        if _oc.is_configured():
            oc = await _srv.asyncio.to_thread(
                _oc.allocation_report, "7d", "namespace",
            )
            if oc is not None:
                if namespace:
                    oc["by_key"] = [r for r in oc["by_key"] if r["name"] == namespace]
                    oc["filtered_to_namespace"] = namespace
                return oc
            _srv.log.info("OpenCost configured but not reachable; using the built-in estimate.")
    except Exception as e:
        _srv.log.debug("OpenCost path skipped: %s", e)

    try:
        connector = KubernetesConnector()
        if not await connector.is_configured():
            return {"error": "No kubeconfig found. Set KUBECONFIG or ensure ~/.kube/config exists."}

        report = connector.analyze_cluster(context)

        # Persist to DB for trend analysis
        try:
            connector.persist_to_db(report)
        except Exception as e:
            _srv.log.warning("Failed to persist k8s data: %s", e)

        # Filter to namespace if requested
        workloads = report.workloads
        if namespace:
            workloads = [w for w in workloads if w.namespace == namespace]

        result: dict = {
            "cluster": report.cluster,
            "provider": report.provider,
            # This path is nable's built-in allocator: node cost from a list-price
            # table split by pod resource share. It is a zero-setup estimate, not
            # your real billed rate, and does not price GPU/network/PV storage.
            # Run OpenCost (set NABLE_OPENCOST_URL) for real, GPU-aware numbers.
            "source": "nable-estimate",
            "is_estimate": True,
            "estimate_note": ("List-price estimate. For real rates including GPU, "
                              "network, and storage, run OpenCost and set NABLE_OPENCOST_URL."),
            "node_count": report.node_count,
            "pod_count": report.pod_count,
            "total_monthly_cost_usd": report.total_monthly_cost,
            "pvc_storage_cost_usd": report.pvc_monthly_cost,
            "wasted_monthly_cost_usd": report.wasted_monthly_cost,
            "waste_pct": round(report.wasted_monthly_cost / report.total_monthly_cost * 100, 1)
                         if report.total_monthly_cost > 0 else 0,
        }

        if report.overall_cpu_efficiency is not None:
            result["cpu_efficiency_pct"] = report.overall_cpu_efficiency
            result["mem_efficiency_pct"] = report.overall_mem_efficiency

        if report.idle_nodes:
            result["idle_nodes"] = report.idle_nodes
            idle_cost = sum(
                n["monthly_cost"] for n in report.node_utilization
                if n["node"] in report.idle_nodes
            )
            result["idle_node_cost_usd"] = round(idle_cost, 2)

        # Cost by namespace
        ns_costs: dict[str, float] = {}
        for w in report.workloads:
            ns_costs[w.namespace] = ns_costs.get(w.namespace, 0) + w.monthly_cost
        ns_sorted = sorted(ns_costs.items(), key=lambda x: x[1], reverse=True)
        result["cost_by_namespace"] = dict(ns_sorted[:50])
        if len(ns_sorted) > 50:
            result["cost_by_namespace_truncated"] = (
                f"Showing top 50 of {len(ns_sorted)} namespaces by spend; "
                f"total_monthly_cost_usd covers all of them."
            )

        # Top workloads
        if len(workloads) > 20:
            result["top_workloads_truncated"] = (
                f"Showing top 20 of {len(workloads)} workloads by listing order; "
                f"total_monthly_cost_usd and cost_by_namespace cover all of them."
            )
        result["top_workloads"] = [
            {
                "namespace": w.namespace,
                "workload": f"{w.workload_kind}/{w.workload_name}",
                "pods": w.pod_count,
                "monthly_cost_usd": w.monthly_cost,
                "wasted_usd": w.wasted_usd,
                "cpu_efficiency_pct": w.cpu_efficiency_pct,
                "mem_efficiency_pct": w.mem_efficiency_pct,
                "labels": w.labels,
            }
            for w in workloads[:20]
        ]

        # Rightsizing opportunities
        if report.rightsizing_opportunities:
            result["rightsizing_opportunities"] = report.rightsizing_opportunities[:10]
            result["total_recoverable_usd"] = round(
                sum(r["potential_savings_usd"] for r in report.rightsizing_opportunities), 2
            )

        # Node utilization summary (cap for large clusters; costliest first)
        nodes_sorted = sorted(
            report.node_utilization,
            key=lambda n: n.get("monthly_cost", 0),
            reverse=True,
        )
        result["node_utilization"] = nodes_sorted[:50]
        if len(nodes_sorted) > 50:
            result["node_utilization_truncated"] = (
                f"Showing 50 costliest of {len(nodes_sorted)} nodes; "
                f"node_count and total_monthly_cost_usd cover all of them."
            )

        # Human-readable summary
        lines = [
            f"Cluster: {report.cluster} ({report.provider.upper()}, {report.node_count} nodes)",
            f"Total cost: ${report.total_monthly_cost:,.0f}/month",
        ]
        if report.wasted_monthly_cost > 10:
            lines.append(
                f"Estimated waste: ${report.wasted_monthly_cost:,.0f}/month "
                f"({result['waste_pct']:.0f}% of cluster cost)"
            )
        if report.overall_cpu_efficiency is not None:
            lines.append(
                f"Efficiency: {report.overall_cpu_efficiency:.0f}% CPU, "
                f"{report.overall_mem_efficiency:.0f}% memory"
            )
        if report.idle_nodes:
            lines.append(
                f"{len(report.idle_nodes)} idle node(s) detected "
                f"(${result.get('idle_node_cost_usd', 0):,.0f}/month)"
            )
        top3_ns = list(result["cost_by_namespace"].items())[:3]
        if top3_ns:
            ns_str = ", ".join(f"{ns}: ${c:,.0f}" for ns, c in top3_ns)
            lines.append(f"Top namespaces: {ns_str}")
        result["summary"] = " | ".join(lines)

        return result

    except Exception as e:
        _srv.log.exception("Kubernetes cost analysis failed")
        return {"error": str(e)}


@_srv.mcp.tool()
async def get_kubernetes_namespace_breakdown(namespace: str) -> dict:
    """
    Deep-dive cost breakdown for a single Kubernetes namespace.
    Shows every workload, pod count, CPU/memory efficiency, and waste.

    Examples:
        - "Break down costs in the production namespace"
        - "Which services in 'data-platform' are most expensive?"
        - "Show me waste in the staging namespace"
    Args:
        namespace: Limit to one Kubernetes namespace. All namespaces when omitted.

    """
    return await _srv.get_kubernetes_costs(namespace=namespace)


@_srv.mcp.tool()
async def connect_opencost() -> dict:
    """
    How to connect nable to OpenCost for real-rate Kubernetes cost, and whether it
    is already connected.

    Without OpenCost, nable's Kubernetes costs are a zero-setup list-price estimate
    that does not price GPU, network, or storage. OpenCost (the CNCF project) prices
    per namespace at your cluster's real rates. nable only READS the OpenCost API;
    it never deploys or changes anything in your cluster. The steps below are for
    you to run.

    Examples:
        - "How do I get accurate Kubernetes costs?"
        - "Connect nable to OpenCost"
        - "Why are my Kubernetes numbers an estimate?"
    """
    from ..connectors import opencost as _oc
    if _oc.is_configured():
        rep = await _srv.asyncio.to_thread(_oc.allocation_report, "1d", "namespace")
        if rep is not None:
            return {
                "connected": True,
                "opencost_url": _oc.opencost_url(),
                "message": ("nable is reading real-rate costs from OpenCost. GPU, network, and "
                            "PV storage are priced at your cluster's actual rates."),
            }
        return {
            "connected": False,
            "opencost_url": _oc.opencost_url(),
            "message": ("NABLE_OPENCOST_URL is set but OpenCost did not respond. Check the URL and "
                        "that the OpenCost pod is running and reachable. nable is using the built-in "
                        "estimate meanwhile."),
        }
    return {
        "connected": False,
        "why": ("Without OpenCost, nable's Kubernetes cost is a zero-setup list-price estimate that "
                "does not price GPU, network, or storage. OpenCost gives real per-namespace rates."),
        "steps": [
            "1. Deploy OpenCost in your cluster (official quickstart):",
            "   kubectl create namespace opencost",
            "   kubectl apply -n opencost -f https://raw.githubusercontent.com/opencost/opencost/develop/kubernetes/opencost.yaml",
            "2. Expose its API to your machine:",
            "   kubectl -n opencost port-forward service/opencost 9003:9003",
            "3. Point nable at it, then re-run your cost question:",
            "   export NABLE_OPENCOST_URL=http://localhost:9003",
        ],
        "note": ("nable only READS the OpenCost API. It never deploys or changes anything in your "
                 "cluster; you run the commands above."),
    }


@_srv.mcp.tool()
async def get_helm_release_costs(
    context: str | None = None,
    namespace: str | None = None,
) -> dict:
    """
    Cost breakdown by Helm release, shows what each release actually costs
    rather than raw deployment names. Detects orphaned releases wasting money.

    Works without the helm CLI, reads release state directly from cluster secrets.

    Examples:
        - "How much does our Prometheus stack cost?"
        - "Which Helm releases are most expensive?"
        - "Do we have any orphaned Helm releases?"
        - "Show me waste broken down by Helm chart"
        - "How much is our ingress controller costing us?"
    Args:
        context: Kubernetes context name from list_kubernetes_contexts(). Default context when omitted.
        namespace: Limit to one Kubernetes namespace. All namespaces when omitted.

    """
    try:
        from ..connectors.kubernetes import KubernetesConnector
        from ..connectors.helm import discover_helm_releases, attribute_costs_to_releases
    except ImportError:
        return {"error": "kubernetes package not installed. Run: pip install finops-mcp[kubernetes]"}

    try:
        connector = KubernetesConnector()
        if not await connector.is_configured():
            return {"error": "No kubeconfig found. Set KUBECONFIG or ensure ~/.kube/config exists."}

        k8s_client = connector._load_client(context)

        # Get workload costs first
        report = connector.analyze_cluster(context)
        workloads = report.workloads
        if namespace:
            workloads = [w for w in workloads if w.namespace == namespace]

        # Discover Helm releases and attribute costs
        releases = discover_helm_releases(k8s_client)
        if namespace:
            releases = [r for r in releases if r.namespace == namespace]

        releases, unmanaged_cost = attribute_costs_to_releases(releases, workloads, k8s_client)

        # Cost by chart (across all releases of same chart)
        by_chart: dict[str, float] = {}
        for r in releases:
            by_chart[r.chart_name] = by_chart.get(r.chart_name, 0) + r.monthly_cost

        orphaned = [r for r in releases if r.is_orphaned]
        orphaned_cost = sum(r.monthly_cost for r in orphaned)

        # Sort detail most-important-first (by cost desc) before capping.
        releases = sorted(releases, key=lambda r: r.monthly_cost, reverse=True)
        release_rows = [
            {
                "name": r.name,
                "namespace": r.namespace,
                "chart": r.chart,
                "chart_name": r.chart_name,
                "chart_version": r.chart_version,
                "app_version": r.app_version,
                "status": r.status,
                "revision": r.revision,
                "deployed_at": r.deployed_at,
                "monthly_cost_usd": r.monthly_cost,
                "wasted_usd": r.wasted_usd,
                "pod_count": r.pod_count,
                "cpu_efficiency_pct": r.cpu_efficiency_pct,
                "workloads": r.workload_names,
                "orphaned": r.is_orphaned,
            }
            for r in releases
        ]
        kept_releases, omitted_releases = _srv.fit_to_budget(release_rows, max_tokens=6000)

        # cost_by_chart can be unbounded on noisy clusters: keep top 50 by cost,
        # but always preserve the grand total over ALL charts.
        sorted_charts = sorted(by_chart.items(), key=lambda x: x[1], reverse=True)

        result = {
            "release_count": len(releases),
            "total_managed_cost_usd": round(sum(r.monthly_cost for r in releases), 2),
            "unmanaged_workload_cost_usd": round(unmanaged_cost, 2),
            "orphaned_release_count": len(orphaned),
            "orphaned_cost_usd": round(orphaned_cost, 2),
            "chart_count": len(sorted_charts),
            "total_chart_cost_usd": round(sum(by_chart.values()), 2),
            "cost_by_chart": {k: round(v, 2) for k, v in sorted_charts[:50]},
            "releases": kept_releases,
        }
        if omitted_releases > 0:
            result["releases_truncated"] = omitted_releases
            result["releases_hint"] = (
                f"showing top {len(kept_releases)} of {len(releases)} releases by cost; "
                f"filter by namespace for full detail"
            )
        if len(sorted_charts) > 50:
            result["cost_by_chart_truncated"] = len(sorted_charts) - 50

        if orphaned:
            orphaned = sorted(orphaned, key=lambda r: r.monthly_cost, reverse=True)
            result["orphaned_releases"] = [
                {
                    "name": r.name,
                    "namespace": r.namespace,
                    "chart": r.chart,
                    "status": r.status,
                    "deployed_at": r.deployed_at,
                    "monthly_cost_usd": r.monthly_cost,
                }
                for r in orphaned[:50]
            ]
            if len(orphaned) > 50:
                result["orphaned_releases_truncated"] = len(orphaned) - 50

        lines = [f"{len(releases)} Helm releases: ${result['total_managed_cost_usd']:,.0f}/month managed"]
        if unmanaged_cost > 10:
            lines.append(f"${unmanaged_cost:,.0f}/month in workloads not managed by Helm")
        if orphaned:
            lines.append(f"⚠️ {len(orphaned)} orphaned release(s) costing ${orphaned_cost:,.0f}/month")
        top3 = sorted(releases, key=lambda r: r.monthly_cost, reverse=True)[:3]
        if top3:
            lines.append("Top: " + ", ".join(f"{r.name} ${r.monthly_cost:,.0f}" for r in top3))
        result["summary"] = " | ".join(lines)

        return result

    except Exception as e:
        _srv.log.exception("Helm cost analysis failed")
        return {"error": str(e)}


@_srv.mcp.tool()
async def estimate_helm_diff_cost(
    diff_text: str,
    release_name: str = "unknown",
    current_replicas: int = 1,
    current_cpu_request: str = "100m",
    current_memory_request: str = "128Mi",
) -> dict:
    """
    Estimate the monthly cost impact of a helm diff or values.yaml change.
    Handles replicaCount, CPU/memory requests, instanceType, and nodeCount changes.

    Paste the output of `helm diff upgrade` or a values.yaml git diff.

    Examples:
        - "How much will this helm diff cost?"
        - "What's the cost impact of scaling from 3 to 10 replicas?"
        - "Estimate cost of upgrading this node pool instance type"
    Args:
        diff_text: Output of `helm diff upgrade ...` to price.
        release_name: Helm release the diff belongs to.
        current_replicas: Current replica count, for delta math.
        current_cpu_request: Current CPU request (e.g. "500m").
        current_memory_request: Current memory request (e.g. "512Mi").

    """
    try:
        from ..connectors.helm import estimate_helm_diff, format_helm_diff_comment
        diff = estimate_helm_diff(
            diff_text=diff_text,
            release_name=release_name,
            current_replica_count=current_replicas,
            current_cpu_request=current_cpu_request,
            current_mem_request=current_memory_request,
        )

        result: dict = {
            "release_name": diff.release_name,
            "delta_monthly_usd": diff.delta_monthly_usd,
            "confidence": diff.confidence,
            "changes": diff.changes,
        }

        if diff.changes:
            direction = "increase" if diff.delta_monthly_usd > 0 else "decrease" if diff.delta_monthly_usd < 0 else "no change"
            result["summary"] = (
                f"Estimated {direction} of ${abs(diff.delta_monthly_usd):,.0f}/month "
                f"for release '{release_name}' (confidence: {diff.confidence})"
            )
            comment = format_helm_diff_comment(diff)
            if comment:
                result["pr_comment"] = comment
        else:
            result["summary"] = "No cost-affecting changes detected in this diff."

        return result

    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def get_cluster_efficiency(context: str | None = None) -> dict:
    """
    Kubernetes cluster efficiency score (0-100) with letter grade, per-namespace
    breakdown, and prioritised recommendations ranked by dollar impact.

    Scores across 4 dimensions:
      - CPU efficiency    (30 pts), actual usage vs requests (needs metrics-server)
      - Memory efficiency (30 pts), actual usage vs requests (needs metrics-server)
      - Idle node penalty (20 pts), penalised for nodes under 10% utilisation
      - Waste ratio       (20 pts), penalised for % of cost that's unrecoverable

    Works without metrics-server, uses request fill-ratio against node capacity.

    Examples:
        - "What's our Kubernetes efficiency score?"
        - "Grade our cluster"
        - "Which namespaces are dragging down our efficiency score?"
        - "Where should we focus to improve cluster efficiency?"
        - "Are we wasting money in Kubernetes?"
    Args:
        context: Kubernetes context name from list_kubernetes_contexts(). Default context when omitted.

    """
    from ..demo_data import is_demo, get_demo_response
    if is_demo():
        return get_demo_response("get_cluster_efficiency") or {}

    try:
        from ..connectors.kubernetes import KubernetesConnector
    except ImportError:
        return {"error": "kubernetes package not installed. Run: pip install finops-mcp[kubernetes]"}

    try:
        connector = KubernetesConnector()
        if not await connector.is_configured():
            return {"error": "No kubeconfig found. Set KUBECONFIG or ensure ~/.kube/config exists."}

        report = connector.analyze_cluster(context)
        result = connector.compute_efficiency_score(report)

        # Human-readable headline
        grade = result["grade"]
        score = result["score"]
        waste = result["wasted_monthly_cost_usd"]
        total = result["total_monthly_cost_usd"]
        grade_msg = {
            "A": "Great shape. Keep rightsizing to hold the grade.",
            "B": "Good, but there's room to claw back $100-500/mo with targeted fixes.",
            "C": "Moderate waste. Tackle idle nodes and top rightsizing candidates first.",
            "D": "Significant over-provisioning. Start with idle nodes and CPU-wasted workloads.",
            "F": "High waste. A dedicated sprint on cluster efficiency will pay for itself in weeks.",
        }.get(grade, "")
        result["headline"] = (
            f"Cluster '{report.cluster}' scores {score:.0f}/100 (Grade {grade}), "
            f"${total:,.0f}/mo total, ${waste:,.0f}/mo estimated waste. {grade_msg}"
        )

        return result
    except Exception as e:
        _srv.log.exception("Cluster efficiency failed")
        return {"error": str(e)}


@_srv.mcp.tool()
async def get_kubernetes_cost_trends(
    days: int = 30,
    cluster: str | None = None,
    namespace: str | None = None,
    granularity: str = "daily",
) -> dict:
    """
    Kubernetes cost trend over time from stored daily snapshots.
    Shows whether cluster spend is growing, shrinking, or stable.

    Snapshots are stored automatically each time get_kubernetes_costs is called.
    The first snapshot date is the start of your trend history.

    Args:
        days:        Lookback window in days (default 30)
        cluster:     Filter to a specific cluster name
        namespace:   Filter to a specific namespace
        granularity: "daily" or "weekly"

    Examples:
        - "Is our Kubernetes spend growing?"
        - "Show me the K8s cost trend for the last 30 days"
        - "How has the production namespace spend changed?"
        - "Is the cluster getting more or less expensive?"
        - "Show weekly Kubernetes cost trends"
    """
    try:
        from ..storage.db import get_engine, kubernetes_costs
        from sqlalchemy import select, func
        from datetime import date, timedelta
    except ImportError:
        return {"error": "Storage not available"}

    try:
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        engine = get_engine()

        with engine.connect() as conn:
            q = select(
                kubernetes_costs.c.snapshot_date,
                kubernetes_costs.c.cluster,
                kubernetes_costs.c.namespace,
                func.sum(kubernetes_costs.c.monthly_cost_usd).label("monthly_cost"),
                func.sum(kubernetes_costs.c.wasted_usd).label("wasted"),
                func.avg(kubernetes_costs.c.cpu_efficiency_pct).label("avg_cpu_eff"),
                func.avg(kubernetes_costs.c.mem_efficiency_pct).label("avg_mem_eff"),
                func.count().label("workload_count"),
            ).where(
                kubernetes_costs.c.snapshot_date >= cutoff,
            )
            if cluster:
                q = q.where(kubernetes_costs.c.cluster == cluster)
            if namespace:
                q = q.where(kubernetes_costs.c.namespace == namespace)

            q = q.group_by(
                kubernetes_costs.c.snapshot_date,
                kubernetes_costs.c.cluster,
                kubernetes_costs.c.namespace,
            ).order_by(kubernetes_costs.c.snapshot_date)

            rows = conn.execute(q).fetchall()

        if not rows:
            return {
                "message": (
                    "No Kubernetes cost history found. "
                    "Run 'get_kubernetes_costs' first to start recording snapshots."
                ),
                "days_requested": days,
                "cluster": cluster,
                "namespace": namespace,
            }

        # Roll up to daily totals across clusters/namespaces
        from collections import defaultdict
        daily: dict[str, dict] = defaultdict(lambda: {
            "date": "",
            "monthly_cost_usd": 0.0,
            "wasted_usd": 0.0,
            "cpu_effs": [],
            "mem_effs": [],
            "workload_count": 0,
        })

        clusters_seen: set[str] = set()
        namespaces_seen: set[str] = set()
        ns_totals: dict[str, float] = {}

        for row in rows:
            d = row.snapshot_date
            daily[d]["date"] = d
            daily[d]["monthly_cost_usd"] += row.monthly_cost or 0
            daily[d]["wasted_usd"] += row.wasted or 0
            daily[d]["workload_count"] += row.workload_count or 0
            if row.avg_cpu_eff is not None:
                daily[d]["cpu_effs"].append(row.avg_cpu_eff)
            if row.avg_mem_eff is not None:
                daily[d]["mem_effs"].append(row.avg_mem_eff)
            clusters_seen.add(row.cluster)
            namespaces_seen.add(row.namespace)
            ns_totals[row.namespace] = ns_totals.get(row.namespace, 0) + (row.monthly_cost or 0)

        # Aggregate to weekly if requested
        trend_points: list[dict] = []
        if granularity == "weekly":
            from itertools import groupby as _gby
            sorted_days = sorted(daily.values(), key=lambda x: x["date"])
            # Group into ISO weeks
            def _week(pt: dict) -> str:
                from datetime import date as _d
                d = _d.fromisoformat(pt["date"])
                return f"{d.isocalendar().year}-W{d.isocalendar().week:02d}"
            for week_key, week_pts in _gby(sorted_days, key=_week):
                pts = list(week_pts)
                all_cpu = [e for p in pts for e in p["cpu_effs"]]
                all_mem = [e for p in pts for e in p["mem_effs"]]
                trend_points.append({
                    "period": week_key,
                    "monthly_cost_usd": round(sum(p["monthly_cost_usd"] for p in pts) / len(pts), 2),
                    "wasted_usd": round(sum(p["wasted_usd"] for p in pts) / len(pts), 2),
                    "avg_cpu_efficiency_pct": round(sum(all_cpu) / len(all_cpu), 1) if all_cpu else None,
                    "avg_mem_efficiency_pct": round(sum(all_mem) / len(all_mem), 1) if all_mem else None,
                    "workload_count": round(sum(p["workload_count"] for p in pts) / len(pts)),
                    "data_points": len(pts),
                })
        else:
            for pt in sorted(daily.values(), key=lambda x: x["date"]):
                cpu_effs = pt.pop("cpu_effs")
                mem_effs = pt.pop("mem_effs")
                trend_points.append({
                    "date": pt["date"],
                    "monthly_cost_usd": round(pt["monthly_cost_usd"], 2),
                    "wasted_usd": round(pt["wasted_usd"], 2),
                    "avg_cpu_efficiency_pct": round(sum(cpu_effs) / len(cpu_effs), 1) if cpu_effs else None,
                    "avg_mem_efficiency_pct": round(sum(mem_effs) / len(mem_effs), 1) if mem_effs else None,
                    "workload_count": pt["workload_count"],
                })

        # Trend direction (computed from the FULL series before any trimming)
        if len(trend_points) >= 2:
            first_cost = trend_points[0]["monthly_cost_usd"]
            last_cost  = trend_points[-1]["monthly_cost_usd"]
            delta_pct  = (last_cost - first_cost) / max(first_cost, 1) * 100
            trend_dir = (
                "growing" if delta_pct > 5 else
                "shrinking" if delta_pct < -5 else "stable"
            )
        else:
            delta_pct = 0.0
            trend_dir = "stable"

        top_ns = sorted(ns_totals.items(), key=lambda x: x[1], reverse=True)[:5]

        # Bound the detail series: a wide window (e.g. days=365 daily) can be
        # hundreds of rows injected into context every turn. Keep summary stats
        # over the FULL series plus the most recent points; never lose totals.
        full_point_count = len(trend_points)
        all_costs = [pt["monthly_cost_usd"] for pt in trend_points]
        all_waste = [pt["wasted_usd"] for pt in trend_points]
        period_summary = {
            "point_count": full_point_count,
            "total_wasted_usd": round(sum(all_waste), 2),
            "min_monthly_cost_usd": round(min(all_costs), 2) if all_costs else 0.0,
            "max_monthly_cost_usd": round(max(all_costs), 2) if all_costs else 0.0,
            "avg_monthly_cost_usd": round(sum(all_costs) / len(all_costs), 2) if all_costs else 0.0,
        }
        trend_truncated = None
        if full_point_count > 45:
            recent_n = 14
            trend_points = trend_points[-recent_n:]
            trend_truncated = (
                f"showing most recent {recent_n} of {full_point_count} "
                f"{granularity} points; see period_summary for full-window stats. "
                f"Use granularity='weekly' or a smaller days window for full detail."
            )

        return {
            "clusters": sorted(clusters_seen),
            "namespaces_in_view": sorted(namespaces_seen) if namespace else None,
            "lookback_days": days,
            "granularity": granularity,
            "data_points": full_point_count,
            "points_shown": len(trend_points),
            "trend_direction": trend_dir,
            "cost_change_pct": round(delta_pct, 1),
            "period_summary": period_summary,
            "trend_truncated": trend_truncated,
            "trend": trend_points,
            "top_namespaces_by_spend": [
                {"namespace": ns, "total_monthly_cost_usd": round(cost, 2)}
                for ns, cost in top_ns
            ],
            "summary": (
                f"K8s cost trend ({days}d): {trend_dir} "
                f"({'up' if delta_pct >= 0 else 'down'} {abs(delta_pct):.1f}% "
                f"from {trend_points[0].get('date') or trend_points[0].get('period', '')} "
                f"to {trend_points[-1].get('date') or trend_points[-1].get('period', '')})"
                if len(trend_points) >= 2 else "Not enough data points for trend yet."
            ),
        }

    except Exception as e:
        _srv.log.exception("K8s cost trend failed")
        return {"error": str(e)}


@_srv.mcp.tool()
async def compare_kubernetes_clusters() -> dict:
    """
    Compare costs and efficiency across all configured Kubernetes clusters.
    Useful for multi-cluster setups (prod vs staging, region vs region).

    Set K8S_CONTEXTS=prod-cluster,staging-cluster to configure.

    Examples:
        - "Compare our Kubernetes clusters"
        - "Which cluster is most efficient?"
        - "Show me spend across all clusters"
    """
    try:
        from ..connectors.kubernetes import KubernetesConnector
    except ImportError:
        return {"error": "kubernetes package not installed. Run: pip install finops-mcp[kubernetes]"}

    try:
        connector = KubernetesConnector()
        reports = connector.analyze_all_clusters()

        if not reports:
            return {"error": "No clusters found. Check K8S_CONTEXTS or KUBECONFIG."}

        comparison = []
        for r in reports:
            comparison.append({
                "cluster": r.cluster,
                "provider": r.provider,
                "nodes": r.node_count,
                "pods": r.pod_count,
                "monthly_cost_usd": r.total_monthly_cost,
                "wasted_usd": r.wasted_monthly_cost,
                "waste_pct": round(r.wasted_monthly_cost / r.total_monthly_cost * 100, 1)
                             if r.total_monthly_cost > 0 else 0,
                "cpu_efficiency_pct": r.overall_cpu_efficiency,
                "namespace_count": len(r.namespaces),
                "idle_nodes": len(r.idle_nodes),
            })

        comparison.sort(key=lambda c: c["monthly_cost_usd"], reverse=True)
        total = sum(c["monthly_cost_usd"] for c in comparison)
        total_waste = sum(c["wasted_usd"] for c in comparison)

        return {
            "clusters": comparison,
            "total_monthly_cost_usd": round(total, 2),
            "total_wasted_usd": round(total_waste, 2),
            "summary": (
                f"{len(reports)} cluster(s): ${total:,.0f}/month total, "
                f"${total_waste:,.0f}/month estimated waste"
            ),
        }

    except Exception as e:
        return {"error": str(e)}


@_srv.mcp.tool()
async def get_databricks_cluster_efficiency() -> dict:
    """
    Audit all Databricks clusters for efficiency issues and cost waste.

    Checks every cluster for:
    - Missing auto-termination (clusters that run forever)
    - Idle clusters (running but no recent activity)
    - Fixed-size clusters that should use autoscaling
    - All-purpose clusters doing batch work (cheaper as job clusters)
    - Clusters with no cost-attribution tags

    Returns a prioritized list of issues and estimated wasted spend.
    Examples:
        - "Which Databricks clusters are inefficient?"

    """
    from ..connectors.databricks import DatabricksConnector

    conn: DatabricksConnector = _srv.SAAS_CONNECTORS.get("databricks")  # type: ignore
    if not conn or not await conn.is_configured():
        return {
            "error": "Databricks not configured. Set DATABRICKS_HOST and DATABRICKS_TOKEN.",
            "help": "Run: finops setup databricks",
        }

    try:
        efficiencies = await conn.get_cluster_efficiency()
    except Exception as e:
        return {"error": str(e)}

    problem_clusters = [e for e in efficiencies if e.issues]
    clean_clusters = [e for e in efficiencies if not e.issues]

    wasted_estimate = sum(
        e.estimated_cost_usd for e in problem_clusters
        if any("idle" in i.lower() or "indefinitely" in i.lower() for i in e.issues)
    )

    issues_out = []
    for e in problem_clusters:
        issues_out.append({
            "cluster": e.cluster_name,
            "state": e.state,
            "type": e.cluster_type,
            "creator": e.creator,
            "estimated_cost": _srv._fmt_usd(e.estimated_cost_usd),
            "uptime_hours": e.uptime_hours,
            "auto_termination_min": e.autotermination_minutes,
            "issues": e.issues,
        })

    return {
        "provider": "databricks",
        "total_clusters": len(efficiencies),
        "clusters_with_issues": len(problem_clusters),
        "clusters_healthy": len(clean_clusters),
        "estimated_waste_usd": _srv._fmt_usd(wasted_estimate),
        "issues": issues_out,
        "healthy_clusters": [
            {"name": e.cluster_name, "type": e.cluster_type, "state": e.state}
            for e in clean_clusters
        ],
    }
