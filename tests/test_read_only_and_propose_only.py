"""nable is read-only and propose-only. These tests enforce it structurally.

Two shipped defects motivated this file:

  1. `cleanup_idle_resources` was advertised to every MCP client with
     readOnlyHint=True / destructiveHint=False while its implementation called
     ec2.delete_volume, ec2.terminate_instances, ec2.release_address,
     ec2.delete_snapshot and elbv2.delete_load_balancer. A client configured to
     auto-approve read-only tools had silently granted mass deletion.
  2. The upgrade-nudge telemetry event sent `savings_found_monthly` and
     `roi_multiple` (= savings / 49, so the figure was recoverable either way)
     while telemetry.py's own "What we never collect" list promises "Cost
     figures or billing data".

Neither would have been caught by a test of the changed function, because in
both cases the function was fine and the CALL SITE was the bug. So these scan
the whole package instead: add a mutating cloud call or a cost-shaped telemetry
key anywhere and one of these fails.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

import finops

PKG = pathlib.Path(finops.__file__).parent


def _py_files() -> list[pathlib.Path]:
    return sorted(PKG.rglob("*.py"))


def _parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


# ── 1. no mutating cloud API call anywhere in the package ────────────────────

# Method names that change or destroy a customer's infrastructure. Deliberately
# unambiguous: every one of these is a cloud-provider SDK verb, so a match is
# real rather than a dict .update() or a local .set_budget().
MUTATING_CALLS = frozenset({
    # EC2 / EBS / networking
    "delete_volume", "delete_snapshot", "terminate_instances", "stop_instances",
    "release_address", "detach_volume", "modify_instance_attribute",
    "delete_nat_gateway", "delete_security_group", "delete_vpc",
    "delete_network_interface", "modify_volume",
    # load balancing
    "delete_load_balancer", "delete_target_group", "deregister_targets",
    # RDS
    "delete_db_instance", "delete_db_cluster", "modify_db_instance",
    "stop_db_instance", "delete_db_snapshot",
    # S3
    "delete_bucket", "delete_object", "delete_objects", "put_bucket_policy",
    # compute / containers / serverless
    "delete_function", "delete_cluster", "delete_service", "update_service",
    "delete_auto_scaling_group", "update_auto_scaling_group",
    "set_desired_capacity", "terminate_instance_in_auto_scaling_group",
    "delete_repository", "batch_delete_image",
    # GCP / Azure equivalents that appear in provider SDKs
    "delete_instance", "delete_disk", "begin_delete", "delete_resource_group",
})

# The only legitimate uses: permission probes that pass DryRun=True and are
# EXPECTED to fail. They tell a user their key is over-provisioned, which is a
# read-only diagnostic, not an action.
DRYRUN_PROBE_FILES = frozenset({"doctor.py", "iam_setup.py"})


def _has_dryrun_true(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg == "DryRun" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
            return True
    return False


def test_no_mutating_cloud_call_survives_in_the_package():
    """The load-bearing test. nable asks the user for read-only credentials; a
    call that mutates is outside the permission they believed they granted."""
    offenders = []
    for path in _py_files():
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in MUTATING_CALLS:
                continue
            if path.name in DRYRUN_PROBE_FILES and _has_dryrun_true(node):
                continue
            offenders.append(f"{path.relative_to(PKG)}:{node.lineno} {node.func.attr}()")
    assert not offenders, (
        "nable must never mutate cloud infrastructure. Propose the command "
        "instead and let the operator run it. Found:\n  " + "\n  ".join(offenders)
    )


def test_the_probe_allowlist_is_not_a_blanket_exemption():
    """The allowlisted files are exempt only for DryRun=True calls. A real
    mutation added to doctor.py must still fail the test above."""
    for name in DRYRUN_PROBE_FILES:
        matches = [p for p in _py_files() if p.name == name]
        assert matches, f"{name} no longer exists; drop it from the allowlist"
    # A DryRun=False call in an allowlisted file is not exempt.
    fake = ast.parse("ec2.terminate_instances(InstanceIds=['i-1'], DryRun=False)")
    call = fake.body[0].value
    assert not _has_dryrun_true(call)


def test_cleanup_actions_does_not_even_import_boto3():
    """Defence in depth: the module that used to delete now has no way to."""
    src = (PKG / "cleanup" / "actions.py").read_text()
    assert "import boto3" not in src
    assert "_boto3_client" not in src


def test_cleanup_proposes_and_never_executes_even_with_dry_run_false(monkeypatch):
    """dry_run=False was the switch that armed deletion. It must now be inert.

    This drives the real entry point with a real IdleResource: if an execution
    path came back, the boto3 import inside it would fail loudly here rather
    than silently deleting a volume on whatever account the runner can reach."""
    from finops.cleanup import actions
    from finops.cleanup.idle import IdleResource

    vol = IdleResource(
        resource_type="ebs_volume", resource_id="vol-0123456789abcdef0",
        region="us-east-1", account_id="111122223333", name="orphan",
        idle_since="2026-06-01", idle_days=60, monthly_cost_usd=8.0,
        reason="unattached for 60 days",
    )
    monkeypatch.setattr(actions, "scan_idle_resources", lambda **kw: [vol])
    monkeypatch.setattr(actions, "_write_audit", lambda *a, **k: None)

    out = actions.cleanup_resources(resource_ids=[], dry_run=False)

    assert out["executed"] is False
    (proposal,) = [p for p in out["proposals"] if p.get("status") == "proposed"]
    assert proposal["command"] == (
        "aws ec2 delete-volume --volume-id vol-0123456789abcdef0 --region us-east-1"
    )
    assert out["estimated_monthly_savings_usd"] == 8.0
    # No status anywhere may claim something happened.
    assert not {p.get("status") for p in out["proposals"]} & {
        "deleted", "released", "terminated"}


def test_protected_resources_are_never_even_proposed(monkeypatch):
    from finops.cleanup import actions
    from finops.cleanup.idle import IdleResource

    vol = IdleResource(
        resource_type="ebs_volume", resource_id="vol-protected",
        region="us-east-1", account_id="1", name="keep", idle_since="2026-06-01",
        idle_days=60, monthly_cost_usd=99.0, reason="idle", protected=True,
    )
    monkeypatch.setattr(actions, "scan_idle_resources", lambda **kw: [vol])
    out = actions.cleanup_resources(resource_ids=[], dry_run=False)
    assert out["skipped_protected"] == 1
    assert out["estimated_monthly_savings_usd"] == 0
    assert all("command" not in p for p in out["proposals"])


def test_the_cleanup_tool_annotation_matches_reality():
    """The annotation is a contract with the MCP client, not a description of
    intent. It may claim read-only only while the test above passes."""
    from finops import tool_surface

    ann = tool_surface.tool_annotation("cleanup_idle_resources")
    assert ann["readOnlyHint"] is True
    assert ann["destructiveHint"] is False
    assert "cleanup_idle_resources" not in tool_surface.WRITE_TOOLS


def test_the_cleanup_tool_docstring_no_longer_teaches_the_model_to_delete():
    """The docstring is the model's whole understanding of the tool. It used to
    say 'Delete the EBS volumes we just listed (then confirm: dry_run=False)',
    which is an instruction to arm the deletion path."""
    from finops import server  # noqa: F401  (tool modules wire up during its import)
    from finops.tools import aws_waste

    fn = getattr(aws_waste.cleanup_idle_resources, "fn", aws_waste.cleanup_idle_resources)
    doc = (fn.__doc__ or "").lower()
    assert "dry_run=false" not in doc
    assert "real action" not in doc
    assert "propose" in doc


# ── 2. no cost figure leaves the machine ─────────────────────────────────────

COST_KEY_HINTS = ("savings", "cost", "spend", "usd", "amount", "dollar",
                  "roi", "bill", "invoice", "budget_amount", "monthly")

# A cardinality is not a currency amount, and telemetry.py explicitly allows
# counts ("Number of connected providers (count only, not which accounts)").
# Without this, `billing_account_count` — len(billing_ids) — trips the `bill`
# hint. Kept deliberately narrow: only the _count suffix, nothing else.
COUNT_SUFFIX = "_count"

TELEMETRY_SINKS = ("_send_event", "capture", "_capture")


def _telemetry_payload_keys(tree: ast.Module) -> list[tuple[int, str]]:
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fname = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        if fname not in TELEMETRY_SINKS:
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            if isinstance(arg, ast.Dict):
                for k in arg.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        found.append((node.lineno, k.value))
    return found


def test_no_telemetry_event_carries_a_figure_derived_from_the_bill():
    """telemetry.py promises, in its own module docstring, never to collect
    'Cost figures or billing data'. The upgrade-nudge event broke that promise
    with savings_found_monthly and roi_multiple."""
    offenders = []
    for path in _py_files():
        if path.name == "telemetry.py":
            continue  # defines the sinks; asserted separately below
        for lineno, key in _telemetry_payload_keys(_parse(path)):
            if key.lower().endswith(COUNT_SUFFIX):
                continue
            if any(h in key.lower() for h in COST_KEY_HINTS):
                offenders.append(f"{path.relative_to(PKG)}:{lineno} sends {key!r}")
    assert not offenders, (
        "Telemetry must not carry anything derived from the user's bill:\n  "
        + "\n  ".join(offenders)
    )


def test_the_promise_this_enforces_is_still_the_documented_one():
    """If someone relaxes the stated policy, this test should be revisited
    deliberately rather than silently enforcing a promise no longer made."""
    doc = (PKG / "telemetry.py").read_text()
    assert "What we never collect" in doc
    assert "Cost figures or billing data" in doc


# ── 3. no authorization gate is computed and thrown away ─────────────────────

GATES = frozenset({"require_role", "require_scope", "check_permission", "require_pro"})


def _discarded_gates(tree: ast.Module) -> list[tuple[int, str]]:
    """A gate used as a bare expression statement. It returns an error dict on
    denial, so discarding the result means the gate does nothing at all."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            f = node.value.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name in GATES:
                out.append((node.lineno, name))
    return out


def test_no_authorization_gate_result_is_discarded():
    """Nine tools called `_srv.require_role("viewer")` as a statement and threw
    the result away. The correct form is `if err := _srv.require_role(...): return err`.

    This reads as protected at a glance, which is what makes it dangerous. It
    only bites in shared-Postgres mode, where roles are actually enforced."""
    offenders = []
    for path in _py_files():
        for lineno, name in _discarded_gates(_parse(path)):
            offenders.append(f"{path.relative_to(PKG)}:{lineno} {name}(...) result discarded")
    assert not offenders, (
        "An authorization gate must decide something. Use "
        "`if err := _srv.require_role(...): return err`:\n  " + "\n  ".join(offenders)
    )


def test_the_gate_scanner_distinguishes_wired_from_discarded():
    """Mutation check: the walrus form must NOT be reported, or the test above
    would be unfixable and someone would delete it."""
    wired = ast.parse("def f():\n    if err := _srv.require_role('admin'):\n        return err\n")
    assert _discarded_gates(wired) == []
    discarded = ast.parse("def f():\n    _srv.require_role('admin')\n")
    assert [n for _, n in _discarded_gates(discarded)] == ["require_role"]


@pytest.mark.parametrize("bad", ["savings_found_monthly", "roi_multiple",
                                 "monthly_cost_usd", "total_spend"])
def test_the_scanner_actually_catches_a_cost_key(bad, tmp_path):
    """Mutation check on the guard itself: a weak matcher would pass silently."""
    f = tmp_path / "leak.py"
    f.write_text(f'_tel._send_event(iid, "e", {{{bad!r}: 1, "context": "x"}})\n')
    keys = _telemetry_payload_keys(ast.parse(f.read_text()))
    assert any(h in k.lower() for _, k in keys for h in COST_KEY_HINTS)
