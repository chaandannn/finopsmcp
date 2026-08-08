"""What a scan will do, before it does any of it.

Asked for directly on r/selfhosted, 2026-08-08: "A dry run listing every required
permission before the first scan would also be useful." It is the right ask. The
current answer to "what will this touch?" is "read the source", and telling a
security-minded operator to trust a claim they cannot check is how a tool gets
declined at review.

Every entry is a check the scanner actually runs, the AWS API calls it makes,
and the IAM action each call needs. All of them are Describe/List/Get: nothing
here mutates, which is the point a reader most wants verified and the reason the
verbs are printed rather than summarised.

The manifest is the single source for both `nable scan --dry-run` and the IAM
policy we hand people, so the policy cannot drift from what the scanner does. A
test asserts the two agree and that no mutating verb ever appears.
"""
from __future__ import annotations

# check -> (what it finds, [(api call, iam action)])
SCAN_CHECKS: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "ebs": ("Unattached volumes, and gp2 that should be gp3", [
        ("ec2.describe_volumes", "ec2:DescribeVolumes")]),
    "snapshots": ("EBS snapshots older than the retention you'd choose", [
        ("ec2.describe_snapshots", "ec2:DescribeSnapshots")]),
    "eips": ("Elastic IPs billing while unassociated", [
        ("ec2.describe_addresses", "ec2:DescribeAddresses")]),
    "nat": ("NAT gateways with no meaningful traffic", [
        ("ec2.describe_nat_gateways", "ec2:DescribeNatGateways"),
        ("cloudwatch.get_metric_statistics", "cloudwatch:GetMetricStatistics")]),
    "ec2": ("Idle and stopped instances still costing money", [
        ("ec2.describe_instances", "ec2:DescribeInstances"),
        ("cloudwatch.get_metric_statistics", "cloudwatch:GetMetricStatistics")]),
    "rds": ("Backup retention beyond what the workload needs", [
        ("rds.describe_db_instances", "rds:DescribeDBInstances")]),
    "rds_rightsizing": ("Oversized database instances, by observed CPU", [
        ("rds.describe_db_instances", "rds:DescribeDBInstances"),
        ("cloudwatch.get_metric_statistics", "cloudwatch:GetMetricStatistics")]),
    "rds_idle": ("Databases with no connections", [
        ("rds.describe_db_instances", "rds:DescribeDBInstances"),
        ("cloudwatch.get_metric_statistics", "cloudwatch:GetMetricStatistics")]),
    "cloudtrail": ("Duplicate trails and expensive data events", [
        ("cloudtrail.describe_trails", "cloudtrail:DescribeTrails"),
        ("cloudtrail.get_event_selectors", "cloudtrail:GetEventSelectors")]),
    "cloudwatch": ("Log groups with no retention set, stored forever", [
        ("logs.describe_log_groups", "logs:DescribeLogGroups")]),
    "s3": ("Buckets on a storage class their access pattern doesn't justify", [
        ("s3.list_buckets", "s3:ListAllMyBuckets"),
        ("s3.get_bucket_lifecycle_configuration", "s3:GetLifecycleConfiguration")]),
    "s3_multipart": ("Incomplete multipart uploads billing silently", [
        ("s3.list_multipart_uploads", "s3:ListBucketMultipartUploads")]),
    "lambda": ("Functions provisioned well above their observed memory", [
        ("lambda.list_functions", "lambda:ListFunctions"),
        ("cloudwatch.get_metric_statistics", "cloudwatch:GetMetricStatistics")]),
    "load_balancer": ("Load balancers with no healthy targets", [
        ("elbv2.describe_load_balancers", "elasticloadbalancing:DescribeLoadBalancers"),
        ("elbv2.describe_target_health", "elasticloadbalancing:DescribeTargetHealth")]),
    "ecr": ("Untagged images nothing has pulled in months", [
        ("ecr.describe_repositories", "ecr:DescribeRepositories"),
        ("ecr.describe_images", "ecr:DescribeImages")]),
    "ecs": ("Fargate tasks reserving far more CPU than they use", [
        ("ecs.list_clusters", "ecs:ListClusters"),
        ("ecs.describe_services", "ecs:DescribeServices"),
        ("cloudwatch.get_metric_statistics", "cloudwatch:GetMetricStatistics")]),
}

# Always needed, whatever checks run: identity, and which regions to sweep.
BASE_ACTIONS: list[tuple[str, str]] = [
    ("sts.get_caller_identity", "sts:GetCallerIdentity"),
    ("ec2.describe_regions", "ec2:DescribeRegions"),
]

# Only on `--spend`, and only because each request is BILLED at $0.01. The
# default scan never calls Cost Explorer, which is the whole reason this is a
# separate list rather than folded into the policy above.
SPEND_ACTIONS: list[tuple[str, str]] = [
    ("ce.get_cost_and_usage", "ce:GetCostAndUsage"),
    ("ce.get_cost_forecast", "ce:GetCostForecast"),
    ("ce.get_dimension_values", "ce:GetDimensionValues"),
]

# A verb that changes anything. The manifest is a promise that none appear, and
# the test enforces it, so a mutating call cannot be added here quietly.
_MUTATING = ("create", "delete", "put", "update", "modify", "terminate",
             "release", "attach", "detach", "start", "stop", "reboot", "run",
             "purchase", "tag", "untag", "set")


def iam_actions(include_spend: bool = False) -> list[str]:
    """Every IAM action a scan needs, sorted and deduplicated."""
    actions = {a for _, a in BASE_ACTIONS}
    for _, calls in SCAN_CHECKS.values():
        actions.update(a for _, a in calls)
    if include_spend:
        actions.update(a for _, a in SPEND_ACTIONS)
    return sorted(actions)


def iam_policy(include_spend: bool = False) -> dict:
    """The least-privilege policy for exactly what the scanner calls."""
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "NableReadOnlyScan",
            "Effect": "Allow",
            "Action": iam_actions(include_spend),
            "Resource": "*",
        }],
    }


def render_dry_run(include_spend: bool = False) -> str:
    """The human-readable manifest printed by `nable scan --dry-run`."""
    out = [
        "nable scan --dry-run",
        "Nothing below was executed. This is what a real scan would call.",
        "",
        "ALWAYS:",
    ]
    for call, action in BASE_ACTIONS:
        out.append(f"  {call:<42} {action}")
    out += ["", f"CHECKS ({len(SCAN_CHECKS)}):"]
    for name, (what, calls) in SCAN_CHECKS.items():
        out.append(f"  {name}")
        out.append(f"      {what}")
        for call, action in calls:
            out.append(f"      {call:<38} {action}")
    if include_spend:
        out += ["", "WITH --spend (each Cost Explorer request is billed $0.01):"]
        for call, action in SPEND_ACTIONS:
            out.append(f"  {call:<42} {action}")
    else:
        out += ["", "Cost Explorer is NOT called. `--spend` adds it, and each",
                "request is billed to your account at $0.01."]
    out += [
        "",
        f"{len(iam_actions(include_spend))} IAM actions, every one a "
        "Describe/List/Get. The scan changes nothing.",
        "",
        "Policy JSON:  nable scan --dry-run --json",
    ]
    return "\n".join(out)
