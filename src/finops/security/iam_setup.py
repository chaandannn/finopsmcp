"""
IAM least-privilege setup for nable (finops-mcp).

Two outputs:
  1. CloudFormation template  — paste into AWS Console or deploy via CLI
  2. Terraform snippet        — drop into your infra repo

Permissions nable needs (strictly read-only):
  Cost Explorer   → ce:Get*, ce:Describe*, ce:List*
  Compute Opt.    → compute-optimizer:Get* (EC2, Lambda, RDS, ECS)
  CloudWatch      → cloudwatch:GetMetricData, GetMetricStatistics, ListMetrics
  EC2 deep audit  → ec2:Describe{Instances,Regions,Volumes,Snapshots,
                     Addresses,NatGateways,Images}
  RDS deep audit  → rds:Describe{DBInstances,DBSnapshots}
  Lambda audit    → lambda:ListFunctions, GetFunctionConfiguration
  CW Logs audit   → logs:DescribeLogGroups, DescribeLogStreams
  CloudTrail      → cloudtrail:DescribeTrails, GetTrailStatus, GetEventSelectors
  S3              → s3:ListAllMyBuckets, GetBucketLocation,
                     GetBucketIntelligentTieringConfiguration
  Organizations   → organizations:List*, Describe* (org rollup)
  STS             → sts:GetCallerIdentity (account id only)

Nothing in this list can create, modify, delete, or terminate any resource. It
is strictly read-only.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

log = logging.getLogger(__name__)

# ── Exact IAM actions required ────────────────────────────────────────────────

_REQUIRED_ACTIONS: list[str] = [
    # Cost Explorer
    "ce:GetCostAndUsage",
    "ce:GetCostForecast",
    "ce:GetReservationUtilization",
    "ce:GetReservationCoverage",
    "ce:GetSavingsPlansPurchaseRecommendation",
    "ce:GetSavingsPlansUtilization",
    "ce:GetSavingsPlansUtilizationDetails",
    "ce:GetSavingsPlansCoverage",
    "ce:GetRightsizingRecommendation",
    "ce:ListCostAllocationTags",
    "ce:DescribeCostCategoryDefinition",
    # Compute Optimizer (deep audit)
    "compute-optimizer:GetEC2InstanceRecommendations",
    "compute-optimizer:GetLambdaFunctionRecommendations",
    "compute-optimizer:GetRDSDatabaseRecommendations",
    "compute-optimizer:GetECSServiceRecommendations",
    "compute-optimizer:GetEnrollmentStatus",
    "compute-optimizer:GetRecommendationSummaries",
    # CloudWatch (rightsizing + deep audit metrics)
    "cloudwatch:GetMetricData",
    "cloudwatch:GetMetricStatistics",
    "cloudwatch:ListMetrics",
    # EC2 (region/instance discovery + deep audit)
    "ec2:DescribeInstances",
    "ec2:DescribeRegions",
    "ec2:DescribeVolumes",
    "ec2:DescribeSnapshots",
    "ec2:DescribeAddresses",
    "ec2:DescribeNatGateways",
    "ec2:DescribeImages",
    # Elastic Load Balancing (idle load balancer detection)
    "elasticloadbalancing:DescribeLoadBalancers",
    # RDS (deep audit — backup retention, utilization)
    "rds:DescribeDBInstances",
    "rds:DescribeDBSnapshots",
    # Lambda (deep audit — memory analysis)
    "lambda:ListFunctions",
    "lambda:GetFunctionConfiguration",
    # ECR (image cleanup recommendations)
    "ecr:DescribeRepositories",
    "ecr:DescribeImages",
    # ECS (service rightsizing)
    "ecs:ListClusters",
    "ecs:ListServices",
    "ecs:DescribeServices",
    "ecs:DescribeTaskDefinition",
    # CloudWatch Logs (retention audit — read only; the fix nable surfaces is a
    # copy-paste `aws logs put-retention-policy` CLI command the user runs
    # themselves, so the connect key needs no write permission)
    "logs:DescribeLogGroups",
    "logs:DescribeLogStreams",
    # CloudTrail (waste pattern detection)
    "cloudtrail:DescribeTrails",
    "cloudtrail:GetTrailStatus",
    "cloudtrail:GetEventSelectors",
    # S3 (storage class + abandoned multipart-upload analysis)
    "s3:ListAllMyBuckets",
    "s3:GetBucketLocation",
    "s3:GetBucketIntelligentTieringConfiguration",
    "s3:ListBucketMultipartUploads",
    "s3:ListMultipartUploadParts",
    # Organizations (org rollup — optional but harmless to include)
    "organizations:ListAccounts",
    "organizations:ListRoots",
    "organizations:ListOrganizationalUnitsForParent",
    "organizations:ListParents",
    "organizations:DescribeOrganizationalUnit",
    "organizations:DescribeOrganization",
    "organizations:DescribeAccount",
    # STS (account id only). sts:AssumeRole is deliberately NOT here: the
    # single-account connect key never assumes a role, and granting AssumeRole on
    # "*" turns a "read-only" key into a privilege-escalation primitive (it can
    # assume any role whose trust policy allows the account root, which is common).
    # Cross-account setups grant AssumeRole separately, scoped to the specific role.
    "sts:GetCallerIdentity",
]

# Actions that WOULD indicate over-provisioned credentials
_DANGEROUS_ACTIONS_PREFIXES = [
    "ec2:Create", "ec2:Delete", "ec2:Modify", "ec2:Run", "ec2:Stop", "ec2:Terminate",
    "s3:Put", "s3:Delete", "s3:Create",
    "iam:Create", "iam:Delete", "iam:Attach", "iam:Detach", "iam:Put", "iam:Update",
    "rds:Create", "rds:Delete", "rds:Modify",
    "lambda:Create", "lambda:Delete", "lambda:Update", "lambda:Invoke",
    "logs:Put", "logs:Create", "logs:Delete",  # logs writes (e.g. PutRetentionPolicy)
    "sts:Assume",  # AssumeRole et al — escalation primitive, not a read
]

CLOUDFORMATION_TEMPLATE: dict[str, Any] = {
    "AWSTemplateFormatVersion": "2010-09-09",
    "Description": (
        "Least-privilege IAM role for nable (finops-mcp). "
        "Grants read access to Cost Explorer, Compute Optimizer, CloudWatch metrics, "
        "EC2/RDS/Lambda/S3/CloudTrail describe APIs, and CloudWatch Logs. "
        "Strictly read-only: no create, modify, or delete permissions of any kind."
    ),
    "Parameters": {
        "RoleName": {
            "Type": "String",
            "Default": "NableFinopsReadOnly",
            "Description": "Name for the IAM role",
        },
        "TrustedAccountId": {
            "Type": "String",
            "Default": "",
            "Description": (
                "AWS account ID allowed to assume this role cross-account (nable's "
                "hosting account for the managed offering). Leave blank for "
                "same-account instance-profile access."
            ),
        },
        "ExternalId": {
            "Type": "String",
            "Default": "",
            "Description": (
                "Optional external ID required on the cross-account assume-role "
                "(confused-deputy protection). Recommended for the managed offering."
            ),
        },
    },
    "Conditions": {
        # Cross-account trust when a TrustedAccountId is supplied; otherwise the role
        # trusts the EC2 service (same-account instance profile) as before.
        "HasTrustedAccount": {"Fn::Not": [{"Fn::Equals": [{"Ref": "TrustedAccountId"}, ""]}]},
        "HasExternalId": {"Fn::Not": [{"Fn::Equals": [{"Ref": "ExternalId"}, ""]}]},
    },
    "Resources": {
        "NableReadOnlyPolicy": {
            "Type": "AWS::IAM::ManagedPolicy",
            "Properties": {
                "ManagedPolicyName": "NableFinopsReadOnlyPolicy",
                "Description": "Exact permissions nable needs — nothing more.",
                "PolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "NableReadOnly",
                            "Effect": "Allow",
                            "Action": _REQUIRED_ACTIONS,
                            "Resource": "*",
                        }
                    ],
                },
            },
        },
        "NableReadOnlyRole": {
            "Type": "AWS::IAM::Role",
            "Properties": {
                "RoleName": {"Ref": "RoleName"},
                "Description": "Read-only role for nable cost intelligence",
                "AssumeRolePolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Fn::If": [
                                "HasTrustedAccount",
                                # Managed offering: only nable's hosting account may
                                # assume this role, optionally gated by an external ID.
                                {
                                    "Effect": "Allow",
                                    "Principal": {
                                        "AWS": {"Fn::Sub": "arn:aws:iam::${TrustedAccountId}:root"}
                                    },
                                    "Action": "sts:AssumeRole",
                                    "Condition": {
                                        "Fn::If": [
                                            "HasExternalId",
                                            {"StringEquals": {"sts:ExternalId": {"Ref": "ExternalId"}}},
                                            {"Ref": "AWS::NoValue"},
                                        ]
                                    },
                                },
                                # Same-account fallback: instance-profile access.
                                {
                                    "Effect": "Allow",
                                    "Principal": {"Service": "ec2.amazonaws.com"},
                                    "Action": "sts:AssumeRole",
                                },
                            ]
                        }
                    ],
                },
                "ManagedPolicyArns": [{"Ref": "NableReadOnlyPolicy"}],
                "Tags": [
                    {"Key": "ManagedBy", "Value": "nable-finops"},
                    {"Key": "Purpose", "Value": "cost-intelligence-read-only"},
                ],
            },
        },
    },
    "Outputs": {
        "RoleArn": {
            "Description": "ARN of the nable read-only role",
            "Value": {"Fn::GetAtt": ["NableReadOnlyRole", "Arn"]},
        },
        "PolicyArn": {
            "Description": "ARN of the nable read-only managed policy",
            "Value": {"Ref": "NableReadOnlyPolicy"},
        },
    },
}


def org_stackset_template() -> "dict[str, Any]":
    """CloudFormation template for the org-wide read-only role, deployed to every
    member account at once via a service-managed StackSet.

    Same least-privilege read set as the single-account role, but the trust policy
    lets the Organizations management (payer) account assume it, so nable (running
    with management-account credentials) can read every child account. Deploy this
    once at the org root and AWS provisions it into all current and future member
    accounts. Cost data does not need this role at all, the payer's Cost Explorer
    already sees every linked account; this is only for per-account resource scans
    (idle, rightsizing, tagging). See setup_aws_org() for the deploy commands.

    RoleName defaults to FinOpsReadOnly to match discover_org_accounts and the
    FINOPS_ORG_ROLE_NAME env var, so nable assumes the same name it deployed.
    """
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": (
            "Org-wide least-privilege read-only role for nable (finops-mcp), one "
            "per member account via a StackSet. Strictly read-only: no create, "
            "modify, or delete of any kind. The management account assumes it to "
            "read each account's resources for waste and rightsizing scans."
        ),
        "Parameters": {
            "ManagementAccountId": {
                "Type": "String",
                "AllowedPattern": "^[0-9]{12}$",
                "Description": "The Organizations management (payer) account ID that nable runs from and that assumes this role.",
            },
            "RoleName": {
                "Type": "String",
                "Default": "FinOpsReadOnly",
                "Description": "Role name created in each member account. Must match what nable assumes (FINOPS_ORG_ROLE_NAME).",
            },
        },
        "Resources": {
            "NableOrgReadOnlyPolicy": {
                "Type": "AWS::IAM::ManagedPolicy",
                "Properties": {
                    "ManagedPolicyName": "NableFinopsOrgReadOnlyPolicy",
                    "Description": "Exact read APIs nable needs in each member account, nothing more.",
                    "PolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": [{
                            "Sid": "NableOrgReadOnly",
                            "Effect": "Allow",
                            "Action": _REQUIRED_ACTIONS,
                            "Resource": "*",
                        }],
                    },
                },
            },
            "NableOrgReadOnlyRole": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "RoleName": {"Ref": "RoleName"},
                    "Description": "Read-only role nable's management account assumes to read this account.",
                    "AssumeRolePolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": [{
                            "Effect": "Allow",
                            "Principal": {"AWS": {"Fn::Sub": "arn:aws:iam::${ManagementAccountId}:root"}},
                            "Action": "sts:AssumeRole",
                        }],
                    },
                    "ManagedPolicyArns": [{"Ref": "NableOrgReadOnlyPolicy"}],
                    "Tags": [
                        {"Key": "ManagedBy", "Value": "nable-finops"},
                        {"Key": "Purpose", "Value": "cost-intelligence-read-only"},
                    ],
                },
            },
        },
        "Outputs": {
            "RoleArn": {
                "Description": "ARN of the nable read-only role in this account",
                "Value": {"Fn::GetAtt": ["NableOrgReadOnlyRole", "Arn"]},
            },
        },
    }


# The role template above needs you to already have AWS credentials on the box to
# assume the role. Most people who get stuck in setup do NOT, so the one-click
# activation path uses this template instead: it mints a read-only IAM user and an
# access key in their own account. They paste the two outputs into the wizard and
# they are connected, with no pre-existing credentials required.
CLOUDFORMATION_TEMPLATE_KEY: dict[str, Any] = {
    "AWSTemplateFormatVersion": "2010-09-09",
    "Description": (
        "Read-only IAM user and access key for nable (finops-mcp). "
        "Scoped to the exact read APIs nable needs (Cost Explorer, Compute "
        "Optimizer, CloudWatch metrics, EC2/RDS/Lambda/S3 describe, CloudTrail, "
        "Logs). No create, modify, or delete permissions of any kind. The access "
        "key id and secret appear in the Outputs tab once: copy them into the "
        "nable setup wizard. You can delete this stack any time to revoke access."
    ),
    "Parameters": {
        "UserName": {
            "Type": "String",
            "Default": "nable-finops-readonly",
            "Description": "Name for the read-only IAM user",
        },
    },
    "Resources": {
        "NableReadOnlyPolicy": {
            "Type": "AWS::IAM::ManagedPolicy",
            "Properties": {
                "ManagedPolicyName": "NableFinopsReadOnlyPolicy",
                "Description": "Exact permissions nable needs — nothing more.",
                "PolicyDocument": {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "NableReadOnly",
                            "Effect": "Allow",
                            "Action": _REQUIRED_ACTIONS,
                            "Resource": "*",
                        }
                    ],
                },
            },
        },
        "NableReadOnlyUser": {
            "Type": "AWS::IAM::User",
            "Properties": {
                "UserName": {"Ref": "UserName"},
                "ManagedPolicyArns": [{"Ref": "NableReadOnlyPolicy"}],
                "Tags": [
                    {"Key": "ManagedBy", "Value": "nable-finops"},
                    {"Key": "Purpose", "Value": "cost-intelligence-read-only"},
                ],
            },
        },
        "NableAccessKey": {
            "Type": "AWS::IAM::AccessKey",
            "Properties": {"UserName": {"Ref": "NableReadOnlyUser"}},
        },
    },
    "Outputs": {
        "NableSetupPaste": {
            "Description": (
                "Copy this ONE value and paste it into the nable setup wizard "
                "(it asks for a single paste first). Treat it as a secret (it "
                "stays visible in this stack's Outputs); delete this stack any "
                "time to revoke the key."
            ),
            "Value": {
                "Fn::Join": [
                    ":",
                    [
                        {"Ref": "NableAccessKey"},
                        {"Fn::GetAtt": ["NableAccessKey", "SecretAccessKey"]},
                    ],
                ]
            },
        },
        "AccessKeyId": {
            "Description": "Only needed if the wizard's single-paste prompt does not accept NableSetupPaste above: paste this as the AWS Access Key ID",
            "Value": {"Ref": "NableAccessKey"},
        },
        "SecretAccessKey": {
            "Description": "Only needed if the wizard's single-paste prompt does not accept NableSetupPaste above: paste this as the AWS Secret Access Key. Treat it as a secret (it stays visible in this stack's Outputs); delete this stack any time to revoke the key.",
            "Value": {"Fn::GetAtt": ["NableAccessKey", "SecretAccessKey"]},
        },
        "PolicyArn": {
            "Description": "ARN of the read-only managed policy attached to this user",
            "Value": {"Ref": "NableReadOnlyPolicy"},
        },
    },
}

# The AWS console's quick-create flow only loads templates from an S3 URL, so the
# key template above is published to a public S3 object (see scripts/publish_cfn.py)
# and the live URL is the default below. Overridable via env for testing or a
# custom bucket. _CFN_TEMPLATE_PLACEHOLDER is kept as the "unpublished" sentinel:
# if the default is ever reset to it, quick_create_available() returns False so the
# wizard never advertises a dead one-click link.
_CFN_TEMPLATE_PLACEHOLDER = "https://nable-public.s3.amazonaws.com/cloudformation/readonly-key.json"
_CFN_TEMPLATE_PUBLISHED = "https://getnable-public.s3.us-east-2.amazonaws.com/cloudformation/readonly-key.json"
CFN_KEY_TEMPLATE_S3_URL = os.environ.get("NABLE_CFN_TEMPLATE_URL", _CFN_TEMPLATE_PUBLISHED)


def quick_create_available() -> bool:
    """True only when a real published template URL is configured (not the
    placeholder), so callers never advertise a one-click link that 404s."""
    return bool(CFN_KEY_TEMPLATE_S3_URL) and CFN_KEY_TEMPLATE_S3_URL != _CFN_TEMPLATE_PLACEHOLDER

_TERRAFORM_TEMPLATE = '''\
# ── nable (finops-mcp) least-privilege IAM role ───────────────────────────────
# Grants read-only access to Cost Explorer, Compute Optimizer, CloudWatch
# metrics, EC2 describe, and Organizations. No write permissions of any kind.

locals {{
  nable_actions = {actions}
}}

resource "aws_iam_policy" "nable_readonly" {{
  name        = "NableFinopsReadOnlyPolicy"
  description = "Exact permissions nable needs — nothing more."

  policy = jsonencode({{
    Version = "2012-10-17"
    Statement = [{{
      Sid      = "NableReadOnly"
      Effect   = "Allow"
      Action   = local.nable_actions
      Resource = "*"
    }}]
  }})

  tags = {{
    ManagedBy = "nable-finops"
    Purpose   = "cost-intelligence-read-only"
  }}
}}

resource "aws_iam_role" "nable_readonly" {{
  name        = "NableFinopsReadOnly"
  description = "Read-only role for nable cost intelligence"

  assume_role_policy = jsonencode({{
    Version = "2012-10-17"
    Statement = [{{
      Effect    = "Allow"
      Principal = {{ Service = "ec2.amazonaws.com" }}
      Action    = "sts:AssumeRole"
    }}]
  }})

  tags = {{
    ManagedBy = "nable-finops"
    Purpose   = "cost-intelligence-read-only"
  }}
}}

resource "aws_iam_role_policy_attachment" "nable_readonly" {{
  role       = aws_iam_role.nable_readonly.name
  policy_arn = aws_iam_policy.nable_readonly.arn
}}

output "nable_role_arn" {{
  description = "ARN of the nable read-only role"
  value       = aws_iam_role.nable_readonly.arn
}}
'''


def generate_cloudformation() -> str:
    """Return CloudFormation template JSON string (read-only role)."""
    return json.dumps(CLOUDFORMATION_TEMPLATE, indent=2)


def generate_cloudformation_key() -> str:
    """Return CloudFormation template JSON string (read-only user + access key).

    This is the template behind the one-click connect link. It mints a scoped
    read-only IAM user and an access key the user pastes into the setup wizard.
    """
    return json.dumps(CLOUDFORMATION_TEMPLATE_KEY, indent=2)


def quick_create_url(region: str = "us-east-1", stack_name: str = "nable-readonly") -> str:
    """One-click AWS console URL that opens the read-only-key stack pre-loaded.

    The user reviews the template (read-only, auditable), clicks Create, then
    copies AccessKeyId and SecretAccessKey from the Outputs tab into the wizard.
    Collapses the IAM step from a dozen console clicks to two copy-pastes, and
    works even when the user has no AWS credentials configured locally.
    """
    from urllib.parse import quote

    template_url = quote(CFN_KEY_TEMPLATE_S3_URL, safe="")
    return (
        f"https://console.aws.amazon.com/cloudformation/home?region={region}"
        f"#/stacks/create/review?templateURL={template_url}"
        f"&stackName={stack_name}"
    )


def generate_terraform() -> str:
    """Return Terraform HCL snippet string."""
    actions_json = json.dumps(_REQUIRED_ACTIONS, indent=4)
    # indent the list to look nice inside the locals block
    indented = "\n".join(
        "  " + line if i > 0 else line
        for i, line in enumerate(actions_json.splitlines())
    )
    return _TERRAFORM_TEMPLATE.format(actions=indented)


# ── Credential scope validator ────────────────────────────────────────────────

def check_credential_scope() -> dict[str, Any]:
    """
    Simulate-call a set of required and dangerous actions via IAM dry-run
    (simulate_principal_policy) to determine whether configured credentials
    are over-provisioned.

    Returns:
        {
            "account_id": str,
            "identity_arn": str,
            "required_allowed": list[str],   # actions that work
            "required_denied": list[str],    # actions that don't work
            "dangerous_allowed": list[str],  # write actions that shouldn't work
            "scoped_correctly": bool,
        }
    """
    try:
        import boto3
    except ImportError:
        return {"error": "boto3 not installed"}

    try:
        sts = boto3.client("sts")
        identity = sts.get_caller_identity()
        account_id = identity["Account"]
        identity_arn = identity["Arn"]
    except Exception as e:
        return {"error": f"Could not get caller identity: {e}"}

    try:
        iam = boto3.client("iam")
        all_actions = _REQUIRED_ACTIONS + [
            "ec2:TerminateInstances",
            "s3:PutObject",
            "iam:CreateUser",
            "lambda:InvokeFunction",
        ]
        resp = iam.simulate_principal_policy(
            PolicySourceArn=identity_arn,
            ActionNames=all_actions,
            ResourceArns=["*"],
        )
        results = {
            r["EvalActionName"]: r["EvalDecision"]
            for r in resp.get("EvaluationResults", [])
        }
    except Exception:
        # simulate_principal_policy requires iam:SimulatePrincipalPolicy
        # which many keys won't have — fall back to a simple allow/deny test
        results = _probe_permissions()

    required_allowed = [a for a in _REQUIRED_ACTIONS if results.get(a) == "allowed"]
    required_denied  = [a for a in _REQUIRED_ACTIONS if results.get(a) != "allowed"]
    dangerous_allowed = [
        a for a in results
        if a not in _REQUIRED_ACTIONS and results[a] == "allowed"
    ]

    return {
        "account_id":       account_id,
        "identity_arn":     identity_arn,
        "required_allowed": required_allowed,
        "required_denied":  required_denied,
        "dangerous_allowed": dangerous_allowed,
        "scoped_correctly": (
            len(required_denied) == 0 and len(dangerous_allowed) == 0
        ),
    }


def _probe_permissions() -> dict[str, str]:
    """
    Fallback: attempt dry-run calls and infer allow/deny from exceptions.
    Only checks a representative subset of actions.
    """
    import boto3
    from botocore.exceptions import ClientError

    results: dict[str, str] = {}

    def _try(fn, action: str) -> None:
        try:
            fn()
            results[action] = "allowed"
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("AccessDenied", "UnauthorizedOperation", "AuthFailure"):
                results[action] = "implicitDeny"
            else:
                # Got a real error back (not auth) — so we're allowed to call it
                results[action] = "allowed"
        except Exception:
            results[action] = "allowed"

    ce = boto3.client("ce")
    _try(
        lambda: ce.get_cost_and_usage(
            TimePeriod={"Start": "2024-01-01", "End": "2024-01-02"},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
        ),
        "ce:GetCostAndUsage",
    )

    ec2 = boto3.client("ec2", region_name="us-east-1")
    _try(
        lambda: ec2.describe_regions(DryRun=True),
        "ec2:DescribeRegions",
    )
    _try(
        lambda: ec2.terminate_instances(InstanceIds=["i-00000000000000000"], DryRun=True),
        "ec2:TerminateInstances",
    )

    s3 = boto3.client("s3")
    try:
        import io
        s3.put_object(Bucket="nable-probe-bucket-does-not-exist", Key="probe", Body=b"")
        results["s3:PutObject"] = "allowed"
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("NoSuchBucket", "AccessDenied"):
            results["s3:PutObject"] = "implicitDeny" if code == "AccessDenied" else "allowed"
        else:
            results["s3:PutObject"] = "allowed"
    except Exception:
        results["s3:PutObject"] = "implicitDeny"

    return results


# ── CLI output ────────────────────────────────────────────────────────────────

def print_iam_template(fmt: str = "cloudformation") -> None:
    """Print IAM template to stdout in requested format."""
    print()
    if fmt == "terraform":
        print("# ── Terraform — copy into your infra repo ──────────────────")
        print(generate_terraform())
    else:
        print("# ── CloudFormation — deploy with: ──────────────────────────")
        print("#   aws cloudformation deploy \\")
        print("#     --template-file nable-iam.json \\")
        print("#     --stack-name nable-readonly \\")
        print("#     --capabilities CAPABILITY_NAMED_IAM")
        print()
        print(generate_cloudformation())

    print()
    print("─" * 60)
    print("  These permissions are strictly read-only. nable cannot create,")
    print("  modify, terminate, or delete any resource with this policy.")
    print("─" * 60)
    print()
