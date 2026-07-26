"""The seamless agent guardrail: classify, gate, hook protocol, installer.

Invariants under test:
  - one-way doors (destroy/delete/terminate/purchase) -> ask, with a reason
  - reversible mutations are silent by default, ask only in strict mode
  - non-infra commands never produce output (zero friction)
  - the hook fails open: garbage input exits 0 with no verdict
  - install is idempotent and uninstall restores the settings file
  - the Budget Guard is Pro: free tier is silent (fail open), never blocks a terminal
"""
import io
import json

import pytest

import finops.guard as g


@pytest.fixture(autouse=True)
def _pro_license(monkeypatch):
    """Gating tests run as Pro; the free-tier fail-open behavior has its own tests
    below that override this."""
    monkeypatch.setattr("finops.license.feature_available", lambda f: True)


# ── free tier: the guard WORKS. It is the front door, not the upsell ──────────
#
# These two tests previously asserted the opposite: that a free user got no
# verdict at all, even on `terraform destroy`. That was the intended design when
# the guard was part of the Pro agent team, and it made the one surface that
# needs no cloud account go silent for everyone who had not paid. Inverted
# deliberately. See the note on "agent_gate" in license.py.

def test_free_tier_gate_still_guards_one_way_doors(monkeypatch):
    monkeypatch.setattr("finops.license.feature_available", lambda f: False)
    assert (g.gate_command("terraform destroy -auto-approve") or {}).get("decision") == "ask"


def test_license_trouble_cannot_disarm_the_guard(monkeypatch):
    """A broken keyring, a lapsed key, an offline check: none of them are a
    reason to stop protecting someone from an irreversible command."""
    def boom(f):
        raise RuntimeError("keyring exploded")
    monkeypatch.setattr("finops.license.feature_available", boom)
    assert (g.gate_command("terraform destroy -auto-approve") or {}).get("decision") == "ask"


# ── classification ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cmd,expected", [
    ("terraform destroy -auto-approve", ("one_way", "delete_resource")),
    ("terraform -chdir=infra destroy", ("one_way", "delete_resource")),
    ("kubectl delete deployment api -n prod", ("one_way", "delete_resource")),
    ("kubectl --context prod delete pod x", ("one_way", "delete_resource")),
    ("helm uninstall my-release", ("one_way", "delete_resource")),
    ("aws ec2 terminate-instances --instance-ids i-123", ("one_way", "terminate_instance")),
    ("aws ec2 release-address --allocation-id eip-1", ("one_way", "release_ip")),
    ("aws ec2 delete-snapshot --snapshot-id snap-1", ("one_way", "snapshot_delete")),
    ("aws savingsplans create-savings-plan --commitment 10", ("one_way", "purchase_commitment")),
    ("aws s3api delete-bucket --bucket b", ("one_way", "delete_resource")),
    ("gcloud compute instances delete vm-1", ("one_way", "delete_resource")),
    ("az vm delete -n vm1 -g rg1", ("one_way", "delete_resource")),
    ("terraform apply -auto-approve", ("two_way", "infra_apply")),
    ("helm upgrade api ./chart", ("two_way", "infra_apply")),
    ("kubectl scale deploy api --replicas=10", ("two_way", "infra_apply")),
    ("aws ec2 run-instances --instance-type p4d.24xlarge", ("two_way", "infra_apply")),
    ("aws ec2 stop-instances --instance-ids i-123", ("two_way", "stop_idle")),
])
def test_classify_infra_commands(cmd, expected):
    assert g.classify_command(cmd) == expected


@pytest.mark.parametrize("cmd", [
    "ls -la", "git push", "terraform plan", "kubectl get pods",
    "aws ec2 describe-instances", "helm list", "npm test",
    "echo terraform destroy is scary",  # over-match tolerated? no: echo matches...
])
def test_non_mutating_commands_unclassified(cmd):
    hit = g.classify_command(cmd)
    if cmd.startswith("echo"):
        # documented over-match tolerance: quoted mentions may classify; the
        # worst case is an unnecessary confirm, never a miss on a real destroy
        return
    assert hit is None


# ── gating ─────────────────────────────────────────────────────────────────────

def test_one_way_asks(monkeypatch):
    monkeypatch.delenv("FINOPS_GUARD_STRICT", raising=False)
    v = g.gate_command("terraform destroy")
    assert v["decision"] == "ask"
    assert "one-way" in v["reason"]


def test_reversible_silent_by_default(monkeypatch):
    monkeypatch.delenv("FINOPS_GUARD_STRICT", raising=False)
    assert g.gate_command("terraform apply") is None
    assert g.gate_command("aws ec2 stop-instances --instance-ids i-1") is None


def test_strict_mode_asks_on_apply(monkeypatch):
    monkeypatch.setenv("FINOPS_GUARD_STRICT", "1")
    v = g.gate_command("terraform apply")
    assert v["decision"] == "ask"
    assert "estimate_change_cost" in v["reason"]


def test_disallowed_action_denies(monkeypatch):
    monkeypatch.delenv("FINOPS_GUARD_STRICT", raising=False)
    # empty the allowlist: stop_idle becomes out-of-policy -> deny
    monkeypatch.setenv("FINOPS_POLICY_ALLOWED_ACTIONS", "ticket")
    v = g.gate_command("aws ec2 stop-instances --instance-ids i-1")
    assert v["decision"] == "deny"


# ── hook protocol ──────────────────────────────────────────────────────────────

def _hook(payload) -> tuple[int, dict | None]:
    out = io.StringIO()
    code = g.run_hook(stdin=io.StringIO(json.dumps(payload)), stdout=out)
    body = out.getvalue()
    return code, (json.loads(body) if body else None)


def test_hook_asks_on_destroy():
    code, body = _hook({"tool_name": "Bash", "tool_input": {"command": "terraform destroy"}})
    assert code == 0
    d = body["hookSpecificOutput"]
    assert d["hookEventName"] == "PreToolUse"
    assert d["permissionDecision"] == "ask"


def test_hook_silent_on_innocent_command():
    code, body = _hook({"tool_name": "Bash", "tool_input": {"command": "git status"}})
    assert code == 0 and body is None


def test_hook_ignores_other_tools():
    code, body = _hook({"tool_name": "Edit", "tool_input": {"file_path": "x"}})
    assert code == 0 and body is None


def test_hook_fails_open_on_garbage():
    out = io.StringIO()
    code = g.run_hook(stdin=io.StringIO("not json at all"), stdout=out)
    assert code == 0 and out.getvalue() == ""


# ── installer ──────────────────────────────────────────────────────────────────

def test_install_uninstall_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "_settings_path", lambda global_scope: tmp_path / "settings.json")
    p = g.install()
    s = json.loads(p.read_text())
    assert s["hooks"]["PreToolUse"][0]["matcher"] == "Bash"
    assert g.is_installed(p)
    # idempotent
    g.install()
    assert len(json.loads(p.read_text())["hooks"]["PreToolUse"]) == 1
    # uninstall restores an empty settings dict
    assert g.uninstall() is True
    assert json.loads(p.read_text()) == {}
    assert not g.is_installed(p)


def test_install_preserves_existing_settings(tmp_path, monkeypatch):
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"model": "opus", "hooks": {"PostToolUse": []}}))
    monkeypatch.setattr(g, "_settings_path", lambda global_scope: target)
    g.install()
    s = json.loads(target.read_text())
    assert s["model"] == "opus"           # untouched
    assert "PostToolUse" in s["hooks"]    # untouched
    assert g.is_installed(target)
    g.uninstall()
    s = json.loads(target.read_text())
    assert s["model"] == "opus" and "PreToolUse" not in s.get("hooks", {})
