"""Spend safety: nable must not spend money, or write into a customer's repo,
without being asked.

Why this file exists. nable is propose-only and read-only, and that promise is
made in four different places with four different mechanisms. Three of them are
open today, and each one fails silently, which is the part that matters: nothing
logs, nothing errors, and the surface that was supposed to stop the action
reports success.

  1. `finops.server.main()` arms nine cron jobs before it serves a single
     request, with no opt-in of any kind (server.py:1381). Adding nable to
     Claude Desktop or Cursor therefore signs the user up for a nightly Cost
     Explorer pull, which AWS bills per request, plus unattended Jira/Linear/
     GitHub ticket creation at 02:00. Every other surface treats the scheduler
     as opt-in and says so in writing: docs/DEPLOY.md, the setup wizard and the
     enterprise dashboard all gate it on FINOPS_ENABLE_SCHEDULER. That variable
     is read nowhere in server.py, so tests/test_mcp_stdio_smoke.py setting it
     to 0 with the comment "no scheduler racing the test" reaches nothing.

  2. The agent guardrail's AWS classifiers hard-code `aws <service> <verb>`
     adjacency (guard.py:48-59), so one global CLI option defeats them.
     `aws ec2 terminate-instances` asks for confirmation; `aws --profile prod
     ec2 terminate-instances` does not, and `--profile` is the normal
     invocation form in exactly the multi-account shops nable sells to. Every
     non-AWS classifier already absorbs intervening tokens. The module's own
     comment at guard.py:34-36 states the intended failure direction:
     over-matching is tolerable, missing a one-way door is not.

  3. `remediation_pr_enabled()` is the declared kill switch for "may nable open
     pull requests in our repositories at all". It is consulted in exactly two
     MCP tool wrappers. The Slack approval path is not one of them
     (slack_bot/remediation.py:338), and when the git step fails nothing rolls
     back the .tf files already written to disk (rightsizing_pr.py:349-371).

Where the seam is. Nothing below replaces a nable function. The scheduler tests
run the real `main()` and stop only at `mcp.run`, the JSON-RPC transport, which
is the process boundary. The guard tests call the shipped classifier and the
shipped hook body with no stubbing at all. The remediation tests run against a
real git repository in tmp_path with a real SQLite database and real Terraform
files, so `open_rightsizing_pr`, the state resolution, the HCL patcher and every
git invocation are the shipped code. The one thing faked is boto3's client
factory in the single test that proves the nightly cron reaches a billed API,
because proving it any other way would mean paying for it.

What this file cannot prove: that AWS still charges for GetCostAndUsage, and
that the AWS CLI still accepts these option forms. Those are facts about a
vendor, not about this code.
"""
from __future__ import annotations

import ast
import io
import json
import logging
import os
import pathlib
import subprocess
import sys
from datetime import datetime, timezone

import boto3
import pytest

import finops.guard as guard
import finops.scheduler.jobs as jobs
import finops.server as srv
import finops.storage.db as db_mod
from finops.auth.rbac import current_identity, set_current_identity

CORE = pathlib.Path(srv.__file__).parent


# ── Isolation ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Every global these tests touch, restored.

    The engine, the data dir and the scheduler are process-wide singletons, and
    `main()` calls logging.basicConfig, so a test that leaves any of them dirty
    changes the result of the next file. The git config vars keep every `git`
    subprocess off the developer's own global config: an insteadOf rewrite or a
    signing requirement there would otherwise decide whether these tests pass.
    """
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "spend-safety.db"))
    # No network from the staleness check, no telemetry, no shared-mode branch.
    monkeypatch.setenv("FINOPS_AIRGAP", "1")
    monkeypatch.setenv("NABLE_NO_TELEMETRY", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "absent-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "absent-gitconfig"))
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    # The PR kill switch defaults to OFF; point the policy file somewhere empty
    # so a nable.policy.yaml in the developer's cwd cannot turn it on.
    monkeypatch.setenv("FINOPS_POLICY_FILE", str(tmp_path / "absent-policy.yaml"))
    for var in ("DATABASE_URL", "FINOPS_API_KEY", "FINOPS_PROFILE",
                "FINOPS_ENABLE_SCHEDULER", "FINOPS_REQUIRE_AUTH",
                "FINOPS_REMEDIATION_ENABLED", "FINOPS_GUARD_STRICT",
                "FINOPS_POLICY_ALLOWED_ACTIONS", "FINOPS_GUARD_PROD_PATTERNS",
                "FINOPS_GUARD_STOP_ON_BUDGET", "GITHUB_TOKEN"):
        monkeypatch.delenv(var, raising=False)

    # The guard's first act is an AI-budget check that reads the developer's own
    # Claude Code session logs. Faked at that subsystem's boundary so these
    # tests measure command classification and nothing else.
    monkeypatch.setattr("finops.ai_budget.status", lambda: {"verdict": "under"})

    prior_engine, prior_dir = db_mod._ENGINE, db_mod._DATA_DIR
    db_mod._ENGINE = None
    db_mod._DATA_DIR = None

    prior_session, prior_note = srv._MCP_SESSION, srv._stale_note
    prior_identity = current_identity()
    root = logging.getLogger()
    prior_level, prior_handlers = root.level, list(root.handlers)

    # Another test file may have left a scheduler running; start_scheduler()
    # returns it untouched, which would mask both the bug and its fix.
    jobs.stop_scheduler()
    try:
        yield
    finally:
        jobs.stop_scheduler()
        db_mod._ENGINE, db_mod._DATA_DIR = prior_engine, prior_dir
        srv._MCP_SESSION, srv._stale_note = prior_session, prior_note
        set_current_identity(prior_identity)
        root.setLevel(prior_level)
        root.handlers[:] = prior_handlers


# ─────────────────────────────────────────────────────────────────────────────
# 1. The stdio MCP entry point arms unattended, billed cron jobs
# ─────────────────────────────────────────────────────────────────────────────

def _run_editor_startup(monkeypatch) -> None:
    """Run the real MCP server startup exactly as an editor launches it.

    No args and piped stdio is the only invocation a Claude Desktop or Cursor
    config produces. `mcp.run` is the JSON-RPC transport loop, the process
    boundary, and the only thing stubbed: everything main() does before it is
    the shipped startup path.
    """
    reached = []
    monkeypatch.setattr(sys, "argv", ["finops-mcp"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(srv.mcp, "run", lambda *a, **k: reached.append(True))
    srv.main()
    assert reached, "main() never reached mcp.run(); this is not the server path"


def _armed_jobs() -> dict:
    sched = jobs._scheduler
    if sched is None or not sched.running:
        return {}
    return {j.id: j for j in sched.get_jobs()}


@pytest.mark.xfail(strict=True, reason="audit finding, not yet fixed. strict=True so that fixing it FAILS here until this marker is removed: the marker count is the work list.")
def test_scheduler_off_must_be_honoured_by_the_mcp_entry_point(monkeypatch):
    """FAILS TODAY. The bug is real.

    A user who set FINOPS_ENABLE_SCHEDULER=0 still gets nine cron jobs, because
    server.py:1381 calls start_scheduler() with no gate and that variable is
    read nowhere in the file. In production this is a nightly
    ce:GetCostAndUsage billed to an account whose owner explicitly opted out,
    plus tickets filed into their tracker at 02:00 with nobody watching.
    """
    monkeypatch.setenv("FINOPS_ENABLE_SCHEDULER", "0")
    _run_editor_startup(monkeypatch)
    assert not _armed_jobs(), (
        "FINOPS_ENABLE_SCHEDULER=0 and the MCP server armed these jobs anyway: "
        f"{sorted(_armed_jobs())}. Each nightly snapshot is a billed Cost "
        "Explorer request on an account whose owner opted out."
    )


@pytest.mark.xfail(strict=True, reason="audit finding, not yet fixed. strict=True so that fixing it FAILS here until this marker is removed: the marker count is the work list.")
def test_an_editor_install_with_no_opt_in_does_not_arm_the_cron(monkeypatch):
    """FAILS TODAY. The bug is real.

    The default case, which is every editor install: no FINOPS_ENABLE_SCHEDULER
    set anywhere, and the stdio server arms the full cron set at startup. What
    breaks in production is that adding nable to Cursor silently starts
    spending the user's money and filing tickets in their tracker, which is not
    what installing a read-only cost tool is understood to mean.

    If the product ever decides unattended billed calls should be the default
    for an editor install, this is the line where that gets decided on purpose
    rather than by omission.
    """
    _run_editor_startup(monkeypatch)
    assert not _armed_jobs(), (
        "a bare editor install armed unattended jobs with no opt-in: "
        f"{sorted(_armed_jobs())}"
    )


def test_an_explicit_opt_in_still_arms_the_scheduler(monkeypatch):
    """PASSES TODAY and must keep passing.

    The pair to the two tests above. Gating the scheduler must not turn into
    deleting it: an operator who asked for FINOPS_ENABLE_SCHEDULER=1, the
    documented self-host and enterprise posture, still needs their digests,
    anomaly alerts and snapshots. If this goes red, the fix went too far and
    every self-hosted box quietly stopped taking snapshots.
    """
    monkeypatch.setenv("FINOPS_ENABLE_SCHEDULER", "1")
    _run_editor_startup(monkeypatch)
    assert _armed_jobs(), (
        "FINOPS_ENABLE_SCHEDULER=1 armed nothing; opting in must still work"
    )


def test_the_cron_it_arms_is_the_billed_and_ticket_filing_pair():
    """PASSES TODAY. Pins what is actually at stake above.

    Records which jobs the scheduler arms, so nobody can argue the tests above
    are about a harmless background timer. `snapshot` is the Cost Explorer path
    and `anomaly` is the one that calls create_ticket. If a rename ever
    detaches these ids from these functions, the tests above are guarding
    something other than what their docstrings claim.
    """
    sched = jobs.start_scheduler()
    assert sched is not None, (
        "could not acquire the scheduler lock even with an isolated "
        "FINOPS_DB_PATH; the test cannot observe what gets armed"
    )
    armed = _armed_jobs()
    assert armed.get("snapshot") is not None
    assert armed["snapshot"].func is jobs.job_snapshot, (
        "the 01:00 job is no longer job_snapshot, which is the Cost Explorer "
        "path the spend-safety tests above are written about"
    )
    assert armed.get("anomaly") is not None
    assert armed["anomaly"].func is jobs.job_detect_and_alert, (
        "the 02:00 job is no longer job_detect_and_alert, which is the path "
        "that auto-creates Jira/Linear/GitHub tickets unattended"
    )


def test_the_snapshot_job_first_act_is_a_billed_cost_explorer_call(monkeypatch):
    """PASSES TODAY. This is the money link that makes the tests above matter.

    job_snapshot -> _snapshot_all -> backfill_from_cost_explorer -> the CE
    GetCostAndUsage that AWS charges for. tests/conftest.py's session guard
    calls the same API "billed per request" and hard-blocks it. Faked at the
    boto3 boundary here, so no request leaves the machine and none is billed.

    If this goes red because the backfill moved, re-read the tests above: they
    assume the armed cron spends money, and that has to stay true or be
    re-established somewhere else.
    """
    calls: list[str] = []

    class _CE:
        def get_cost_and_usage(self, **kw):
            calls.append("ce:GetCostAndUsage")
            return {"ResultsByTime": []}

    class _STS:
        def get_caller_identity(self):
            return {"Account": "111122223333"}

    monkeypatch.setattr(
        boto3, "client", lambda name, **kw: _CE() if name == "ce" else _STS()
    )

    from finops.anomaly.backfill import backfill_from_cost_explorer, needs_backfill

    assert needs_backfill(), (
        "a fresh install has no history, so the backfill must want to run; "
        "otherwise this test proves nothing"
    )
    backfill_from_cost_explorer()
    assert calls == ["ce:GetCostAndUsage"], (
        f"expected exactly one billed Cost Explorer request, got {calls}"
    )
    import inspect
    assert "backfill_from_cost_explorer" in inspect.getsource(jobs._snapshot_all), (
        "the nightly snapshot job no longer calls the backfill, so the chain "
        "this test documents is broken"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. The agent guardrail and AWS global options
# ─────────────────────────────────────────────────────────────────────────────

# Every one of these ends a resource's life or signs a one-year financial
# commitment. The tail is everything after `aws`, so the tests can insert a
# global option in the one place that breaks the classifier.
_AWS_ONE_WAY_DOORS = [
    ("ec2 terminate-instances --instance-ids i-0abc", "terminate_instance"),
    ("ec2 release-address --allocation-id eipalloc-1", "release_ip"),
    ("ec2 delete-snapshot --snapshot-id snap-1", "snapshot_delete"),
    ("ec2 delete-volume --volume-id vol-1", "delete_resource"),
    ("s3 rm s3://acme-prod-data --recursive", "delete_resource"),
    ("s3 rb s3://acme-prod-data --force", "delete_resource"),
    ("savingsplans create-savings-plan --savings-plan-offering-id sp-1 "
     "--commitment 40000", "purchase_commitment"),
]

# Documented `aws` global options. Any of them may precede the service name.
_AWS_GLOBAL_OPTIONS = [
    "--profile prod",
    "--region us-east-1",
    "--output json",
    "--no-cli-pager",
    "--endpoint-url https://vpce-abc.ec2.us-east-1.vpce.amazonaws.com",
]


@pytest.mark.parametrize("tail,action", _AWS_ONE_WAY_DOORS)
def test_the_same_aws_command_is_classified_without_a_global_option(tail, action):
    """PASSES TODAY. The control for the test below.

    Establishes that the verb list itself is correct, so when the parametrized
    test below goes red the only difference is the option, not the command.
    """
    assert guard.classify_command(f"aws {tail}") == ("one_way", action)


@pytest.mark.parametrize("tail,action", _AWS_ONE_WAY_DOORS)
def test_a_profile_flag_does_not_hide_an_aws_one_way_door(tail, action):
    """FAILS TODAY. The bug is real.

    `--profile` is how anyone with more than one AWS account invokes the CLI,
    which is every customer nable is sold to. Today the guard sees
    `aws --profile prod ec2 terminate-instances`, matches nothing, and the
    agent terminates a production instance with no confirmation and no record.
    The same command without `--profile` is stopped, so the failure is invisible
    to anyone who tested the guard on a single-account laptop.
    """
    got = guard.classify_command(f"aws --profile prod {tail}")
    assert got == ("one_way", action), (
        f"`aws --profile prod {tail}` classified as {got!r}. A global CLI "
        "option must not turn a one-way door into an unguarded command."
    )


@pytest.mark.parametrize("option", _AWS_GLOBAL_OPTIONS)
def test_no_aws_global_option_form_hides_a_terminate(option):
    """FAILS TODAY. The bug is real.

    One option form is not the whole hole. Each of these is a documented `aws`
    global option that legitimately sits between the binary and the service
    name, and each one currently makes terminate-instances invisible to the
    guard. In production that is an agent destroying an instance because the
    operator happened to pin a region on the command line.
    """
    cmd = f"aws {option} ec2 terminate-instances --instance-ids i-0abc"
    assert guard.classify_command(cmd) == ("one_way", "terminate_instance"), (
        f"{cmd!r} was not recognised as a one-way door"
    )


@pytest.mark.parametrize("cmd,action", [
    ("kubectl --context prod delete deployment api", "delete_resource"),
    ("gcloud --project acme-prod compute instances delete vm-1", "delete_resource"),
    ("az --subscription 0000 vm delete -n vm1 -g rg1", "delete_resource"),
    ("terraform -chdir=infra destroy", "delete_resource"),
])
def test_the_non_aws_classifiers_already_absorb_global_options(cmd, action):
    """PASSES TODAY and must keep passing.

    This is the shape the AWS patterns are missing, and the proof the fix is a
    small one: every other tool's pattern already tolerates tokens between the
    binary and the verb. If this ever goes red, the guard has lost the one
    family of one-way doors it currently gets right.
    """
    assert guard.classify_command(cmd) == ("one_way", action)


def _hook(command: str) -> tuple[int, dict | None]:
    """Drive the real PreToolUse hook body the way Claude Code drives it."""
    out = io.StringIO()
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    code = guard.run_hook(stdin=io.StringIO(json.dumps(payload)), stdout=out)
    body = out.getvalue()
    return code, (json.loads(body) if body else None)


def test_the_hook_asks_before_a_terminate_that_carries_a_profile_flag():
    """FAILS TODAY. The bug is real.

    The end of the chain, and the only part a user ever sees. `finops guard
    install` wires this hook into Claude Code so the agent cannot run a
    one-way door without a human nod. With `--profile prod` on the command the
    hook writes nothing and exits 0, which Claude Code reads as "no opinion",
    so the agent proceeds. A user who installed the guard believes they are
    protected in exactly the account where they are not.
    """
    code, body = _hook("aws --profile prod ec2 terminate-instances --instance-ids i-0abc")
    assert code == 0, "the hook must always exit 0; it fails open by design"
    assert body is not None, (
        "the hook stayed silent on a production terminate-instances, so the "
        "agent runs it unprompted"
    )
    assert body["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_the_hook_asks_before_a_terminate_with_no_profile_flag():
    """PASSES TODAY and must keep passing.

    The control: the guard's confirmation path works, and the fix must not
    break it while widening the patterns.
    """
    code, body = _hook("aws ec2 terminate-instances --instance-ids i-0abc")
    assert code == 0 and body is not None
    assert body["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_an_innocent_aws_command_stays_silent_with_a_profile_flag():
    """PASSES TODAY and must keep passing.

    The other direction of the fix. Absorbing global options must not turn
    `aws --profile prod ec2 describe-instances` into a confirmation prompt:
    a guard that interrupts read-only commands gets uninstalled, and then it
    protects nobody.
    """
    assert guard.classify_command("aws --profile prod ec2 describe-instances") is None
    assert guard.classify_command("aws --region us-east-1 s3 ls s3://acme") is None


# ─────────────────────────────────────────────────────────────────────────────
# 3 and 4. Writing into the customer's repository
# ─────────────────────────────────────────────────────────────────────────────

_TF_BEFORE = 'resource "aws_instance" "api" {\n  instance_type = "m5.xlarge"\n}\n'

_TF_STATE = {
    "version": 4,
    "resources": [{
        "type": "aws_instance", "name": "api",
        "instances": [{"attributes": {"id": "i-0abc", "instance_type": "m5.xlarge"}}],
    }],
}


def _git(repo, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)


@pytest.fixture()
def iac_repo(tmp_path):
    """A real git repository holding real Terraform, the way a customer's is.

    No remote is configured, so the push step fails locally at "'origin' does
    not appear to be a git repository" and nothing reaches the network. Every
    step before the push, which is where the damage in these findings happens,
    runs for real.
    """
    repo = tmp_path / "acme-infra"
    repo.mkdir()
    (repo / "main.tf").write_text(_TF_BEFORE)
    (repo / "terraform.tfstate").write_text(json.dumps(_TF_STATE))
    for args in (("init", "-q"),
                 ("config", "user.email", "nobody@example.invalid"),
                 ("config", "user.name", "test"),
                 ("config", "commit.gpgsign", "false"),
                 ("add", "-A"),
                 ("commit", "-q", "-m", "initial infra")):
        r = _git(repo, *args)
        assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return repo


@pytest.fixture()
def open_rightsizing_recommendation():
    """One open rightsizing recommendation in a real database.

    Written through the shipped table definition rather than a stub, so
    open_rightsizing_pr's own SELECT is the code that reads it.
    """
    from finops.storage.db import get_engine, savings_recommendations

    with get_engine().begin() as conn:
        conn.execute(savings_recommendations.insert().values(
            source="rightsizing", provider="aws", account_id="111122223333",
            region="us-east-1", resource_id="i-0abc", resource_type="ec2",
            resource_name="api",
            current_config=json.dumps({"instance_type": "m5.xlarge"}),
            recommended_config=json.dumps({
                "tf_resource_type": "aws_instance",
                "tf_resource_name": "api",
                "instance_type": "m5.large",
                "from_instance_type": "m5.xlarge",
            }),
            description="m5.xlarge is oversized for its measured load",
            estimated_monthly_savings_usd=70.08,
            status="open",
            # Naive UTC, matching what the shipped writers store in this column.
            generated_at=datetime.now(timezone.utc).replace(tzinfo=None),
            dedup_key="spend-safety-rightsizing-1",
        ))


def _approve_from_slack(repo) -> dict:
    """Drive the real Slack approval path: draft a pending action, then approve
    it as a second person with the analyst role, exactly as the button handler
    does. Nothing here is stubbed."""
    from finops.slack_bot import remediation as slack_remediation

    action_id = slack_remediation._create_pending(
        kind="rightsizing_pr",
        payload={"tf_dir": str(repo), "github_repo": "acme/infra",
                 "recommendation_ids": None, "branch": "fix/rightsizing"},
        preview="Terraform rightsizing PR",
        requested_by="U_ALICE",
    )
    return slack_remediation.approve_action(
        action_id, resolved_by="U_BOB", role="analyst"
    )


@pytest.mark.xfail(strict=True, reason="audit finding, not yet fixed. strict=True so that fixing it FAILS here until this marker is removed: the marker count is the work list.")
def test_slack_approval_does_not_write_to_the_repo_while_prs_are_disabled(
    iac_repo, open_rightsizing_recommendation
):
    """FAILS TODAY. The bug is real.

    remediation_pr_enabled() is the switch a security team is told to use when
    they ask "can nable open pull requests in our repos?". It defaults to OFF
    and it is consulted in two MCP tool wrappers only. The Slack approval
    handler is not one of them, so a click in Slack patches the customer's .tf
    files, creates a branch and commits, and would push it if a remote existed.
    Here the push is the only step that fails, and only because this fixture
    has no remote.

    What breaks in production: a company that reviewed nable.policy.yaml and
    signed off on "nable may not open PRs in our repositories" gets PRs. The
    two gates are inversely correlated, which makes it worse: drafting turns
    itself on whenever FINOPS_REQUIRE_AUTH=1, which is what server.py:1375
    tells enterprises to set.
    """
    from finops.remediation.gate import remediation_pr_enabled
    assert remediation_pr_enabled() is False, "fixture setup: the gate must be off"

    result = _approve_from_slack(iac_repo)

    assert (iac_repo / "main.tf").read_text() == _TF_BEFORE, (
        "the approval handler rewrote the customer's Terraform while the "
        "declared remediation kill switch was off"
    )
    assert not _git(iac_repo, "branch", "--list", "fix/rightsizing").stdout.strip(), (
        "the approval handler created a branch in the customer's repository "
        "while the declared remediation kill switch was off"
    )
    blob = json.dumps(result).lower()
    assert "remediation" in blob and result.get("error"), (
        f"the refusal must name the gate so an operator can find it, got {result!r}"
    )


def test_enabling_the_gate_still_lets_an_approved_pr_proceed(
    iac_repo, open_rightsizing_recommendation, monkeypatch
):
    """PASSES TODAY and must keep passing.

    The pair to the test above, and the reason it cannot be satisfied by
    refusing everything. An operator who set FINOPS_REMEDIATION_ENABLED=true
    has opted in, and the approval flow must still patch the file and create
    the branch. If this goes red the fix turned an opt-in gate into a wall and
    the remediation feature is dead.
    """
    monkeypatch.setenv("FINOPS_REMEDIATION_ENABLED", "true")
    _approve_from_slack(iac_repo)

    assert "m5.large" in (iac_repo / "main.tf").read_text(), (
        "an explicitly enabled remediation did not patch the Terraform"
    )
    assert _git(iac_repo, "branch", "--list", "fix/rightsizing").stdout.strip(), (
        "an explicitly enabled remediation did not create the branch"
    )


@pytest.mark.xfail(strict=True, reason="audit finding, not yet fixed. strict=True so that fixing it FAILS here until this marker is removed: the marker count is the work list.")
def test_every_ungated_pr_call_site_is_covered_by_the_kill_switch():
    """FAILS TODAY. The bug is real, and this is the test that keeps it fixed.

    A grep-shaped invariant, because the defect is a missing call rather than a
    wrong one and no behavioural test covers a call site nobody has written
    yet. Any module that opens a PR for real, meaning a non-dry-run
    open_rightsizing_pr or a direct create_github_pr, must reference the gate,
    unless the chokepoint function itself enforces it, which is the better fix.

    Today slack_bot/remediation.py:341 is the one offender. What breaks in
    production if this goes red again: a fourth copy of this call site ships
    with the kill switch inert and nobody notices until a customer finds a
    branch in their repo.
    """
    writers = {
        "open_rightsizing_pr": CORE / "remediation" / "rightsizing_pr.py",
        "create_github_pr": CORE / "integrations" / "ticketing.py",
    }
    # A chokepoint that gates itself covers every caller, so drop those names.
    ungated_writers = {
        name for name, home in writers.items()
        if "remediation_pr_enabled" not in home.read_text()
    }

    def _callee(node: ast.Call) -> str:
        f = node.func
        if isinstance(f, ast.Name):
            return f.id
        if isinstance(f, ast.Attribute):
            return f.attr
        return ""

    def _kw_is_true(node: ast.Call, name: str) -> bool:
        for k in node.keywords:
            if k.arg == name:
                return isinstance(k.value, ast.Constant) and k.value.value is True
        return False

    offenders = []
    for path in sorted(CORE.rglob("*.py")):
        if path in writers.values():
            continue          # the chokepoints themselves
        source = path.read_text()
        if "remediation_pr_enabled" in source:
            continue          # this module consults the gate
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _callee(node)
            if name not in ungated_writers:
                continue
            if name == "open_rightsizing_pr" and (
                _kw_is_true(node, "dry_run") or _kw_is_true(node, "patch_only")
            ):
                continue      # diff-only and local-only never leave the box
            offenders.append(f"{path.relative_to(CORE.parent)}:{node.lineno} {name}()")

    assert not offenders, (
        "these call sites push a branch or open a pull request in a customer's "
        "repository without consulting remediation_pr_enabled(): "
        + ", ".join(offenders)
    )


@pytest.mark.xfail(strict=True, reason="audit finding, not yet fixed. strict=True so that fixing it FAILS here until this marker is removed: the marker count is the work list.")
def test_a_failed_git_step_leaves_no_edit_in_the_customers_working_tree(
    iac_repo, open_rightsizing_recommendation, monkeypatch
):
    """FAILS TODAY. The bug is real.

    Step 3 of open_rightsizing_pr writes new instance types into the
    customer's .tf files. Step 5 then runs `git checkout -b fix/rightsizing`,
    and that branch name is a hard-coded default (rightsizing_pr.py:137), so
    the second run against the same repo always fails with "a branch named
    fix/rightsizing already exists". This fixture reproduces exactly that
    state: a branch left behind by an earlier run.

    Nothing rolls back. The error is returned, and nable's uncommitted edit is
    left sitting in the working tree on whatever branch the user was on, which
    is usually main. If anyone then runs `terraform apply` from that directory,
    an unreviewed instance-type change lands on production infrastructure. The
    same hazard applies to a push failure: no upstream, an expired token, a
    protected branch.
    """
    monkeypatch.setenv("FINOPS_REMEDIATION_ENABLED", "true")
    assert _git(iac_repo, "branch", "fix/rightsizing").returncode == 0
    starting_branch = _git(iac_repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

    from finops.remediation.rightsizing_pr import open_rightsizing_pr
    result = open_rightsizing_pr(tf_dir=str(iac_repo), github_repo=None, dry_run=False)

    assert result.get("error"), "fixture setup: the git step was supposed to fail"
    assert "already exists" in result["error"], (
        f"fixture setup: expected the branch collision, got {result['error']!r}"
    )
    assert _git(iac_repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == starting_branch

    dirty = _git(iac_repo, "status", "--porcelain").stdout.strip()
    assert (iac_repo / "main.tf").read_text() == _TF_BEFORE, (
        "a failed git step left nable's instance-type edit in the customer's "
        f"working tree: `git status --porcelain` says {dirty!r}. The next "
        "`terraform apply` in that directory applies a change no human reviewed."
    )
    assert not dirty, f"the working tree was left dirty: {dirty!r}"


def test_the_happy_path_still_patches_and_commits(
    iac_repo, open_rightsizing_recommendation, monkeypatch
):
    """PASSES TODAY and must keep passing.

    The pair to the test above. Rolling back on failure must not turn into
    rolling back on success: with the gate on, a clean repo and no branch
    collision, the patch has to land and be committed. Only the push fails
    here, because this fixture deliberately has no remote.
    """
    monkeypatch.setenv("FINOPS_REMEDIATION_ENABLED", "true")

    from finops.remediation.rightsizing_pr import open_rightsizing_pr
    result = open_rightsizing_pr(tf_dir=str(iac_repo), github_repo=None, dry_run=False)

    assert "origin" in (result.get("error") or ""), (
        f"expected the push to be the only failing step, got {result!r}"
    )
    assert "m5.large" in (iac_repo / "main.tf").read_text()
    assert _git(iac_repo, "branch", "--list", "fix/rightsizing").stdout.strip()
    assert "rightsizing" in _git(iac_repo, "log", "-1", "--format=%s").stdout


def test_a_dry_run_never_touches_the_repository(
    iac_repo, open_rightsizing_recommendation
):
    """PASSES TODAY and must keep passing.

    The escape hatch the disabled-gate message points users at: dry_run shows
    the diff and touches nothing outside the box. It is the honest answer for
    anyone who wants the recommendation without the write, so it has to stay
    true whatever the gate does, and it must keep working with the gate off.
    """
    from finops.remediation.rightsizing_pr import open_rightsizing_pr
    result = open_rightsizing_pr(tf_dir=str(iac_repo), github_repo=None, dry_run=True)

    assert result.get("dry_run") is True and result.get("diffs")
    assert (iac_repo / "main.tf").read_text() == _TF_BEFORE
    assert not _git(iac_repo, "status", "--porcelain").stdout.strip()
    assert not _git(iac_repo, "branch", "--list", "fix/rightsizing").stdout.strip()
