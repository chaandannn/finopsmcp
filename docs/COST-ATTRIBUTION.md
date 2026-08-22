# Cost attribution: splitting the bill by team

How nable decides which team owns which dollar, what it needs from you, and what
it cannot do yet.

## The short version

nable reads the tags **already on your resources** out of the billing data your
cloud produces, and maps them to a team, a service and an environment using rules
you write. It does not need Terraform, it does not install an agent, and it does
not tag anything on your behalf.

If your resources are tagged and those tags reach your bill, attribution works
with one config file.

## What you need

**Tags on your resources.** Any key you like. nable does not require a naming
convention.

**Those tags reaching the billing data.** This is the step people miss, and it is
covered in its own section below.

**A rules file** at `~/.finops/tag_rules.yaml` (override with `FINOPS_TAG_RULES`).
Generate a starting point:

```
nable tags
```

## Writing rules

```yaml
rules:
  # Most specific first. Lower priority number wins.
  - tag_key: "team"
    tag_value_pattern: "infra*"     # glob against the tag VALUE
    maps_to_field: "team"
    maps_to_value: "platform"       # force a canonical name
    priority: 5

  - tag_key: "team"
    maps_to_field: "team"
    priority: 10

  # Fallback: only consulted when no higher-priority rule matched this field.
  - tag_key: "costcenter"
    maps_to_field: "team"
    priority: 50

team_aliases:
  platform: [infra, infrastructure, platform-eng, sre]
  data: [analytics, ml, ml-platform, data-eng]
```

**The first rule that matches a field wins.** Rules are sorted by `priority`,
lowest number first, so put specific rules at low numbers and fallbacks at high
ones. Once a field is decided, later rules for that field are skipped, which is
what makes fallbacks safe to leave in place.

Each field is decided independently. A rule settling `team` does not stop
`service` or `environment` from resolving.

`maps_to_field` accepts `team`, `service`, or `environment`. Anything with no
matching rule comes back as `unattributed`.

> **Changed in 0.8.216.** `priority` previously worked backwards: the highest
> number won, so a `costcenter` fallback could overwrite an explicit `team` tag.
> If you wrote rules against the old behaviour your numbers will shift, and they
> shift toward what your rules say.

## Why your attribution might be empty

### AWS: tags must be activated for cost allocation

A tag on a resource does not appear in Cost Explorer or the CUR until you
activate it as a **cost allocation tag** in Billing. It is a separate step, in a
different console, and nothing warns you.

Billing and Cost Management → Cost allocation tags → select your keys → Activate.

Two things worth knowing:

- Activation applies **going forward only**. History before you switch it on
  cannot be attributed later, so do this early.
- Keys take up to 24 hours to start appearing.

When attribution comes back empty, nable checks which of your configured keys are
active and tells you which are not. That check calls the Cost Explorer API, which
AWS bills per request, so it runs only when a person asked a question that came
back empty. It never runs on a schedule, in demo mode, or under
`NABLE_NO_COST_EXPLORER=1`.

### Azure

Tags on a resource do not flow to cost records from the resource group or
subscription unless you enable the tag inheritance policy in Cost Management.
Without it, only tags set directly on the resource appear.

### GCP

Labels work on the resource. Project and folder labels are not inherited onto
line items.

## Fixing tags at the source

Separate from reading tags, nable can find the resources missing them and fix
them in your IaC:

- `audit_terraform_tags` lists Terraform resources missing required tags
- `generate_terraform_tag_fixes` produces the patch
- `open_terraform_tag_pr` opens a pull request with it

This is optional and Terraform-only. Opening PRs is off by default; see
`FINOPS_REMEDIATION_ENABLED` and `nable.policy.yaml`.

For the reporting side, `get_untagged_resource_cost` tells you how much spend is
missing a given tag and which services are the worst offenders, which is usually
the more useful number when you are trying to get a tagging push funded.

## What this does not do yet

Being direct about the limits, because they matter when you are comparing tools.

**Rules match on tags only.** You cannot currently write "account 4471 is
platform" or "resources named `prod-api-*` are backend". If a resource is
untagged, it is `unattributed`, and reporting is all you get.

**No shared cost allocation.** There is no way to split a shared cluster, a NAT
gateway, or a Datadog bill across teams by some ratio. Shared costs stay in
`unattributed`.

**Three fixed dimensions.** `team`, `service`, `environment`. No product,
feature, customer, or business unit, which also means no cost-per-customer.

**No account or subscription hierarchy.** Nothing falls back from resource tags
to the account, subscription, project, or OU that contains the resource.

If any of these block you, that is useful signal and worth telling us.

## Related tools

| Tool | What it answers |
|---|---|
| `get_costs_by_team` | Spend split by team over a period |
| `get_tag_cost_breakdown_cur` | Spend by any tag key, straight from the CUR |
| `get_untagged_resource_cost` | How much spend is missing a tag, by service |
| `get_team_scorecards` | Efficiency scores per team |
| `get_commitment_coverage_by_tag` | RI and Savings Plan coverage by tag |
| `run_attribution_now` | Recompute attribution against current rules |
