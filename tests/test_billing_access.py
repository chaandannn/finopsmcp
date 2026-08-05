"""Cost Explorer is not a data source, and the gate that enforces that.

nable reads cost from the billing export each cloud already produces. Cost
Explorer bills the customer per request and cannot return line items, so it is
off by default and every direct client construction is being retired.

These tests are written to BREAK the gate, not to demonstrate it. A gate that
only fails closed on the inputs its author thought of is not a gate.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

import finops
from finops import billing_access as ba

PKG = pathlib.Path(finops.__file__).parent


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in (ba.ALLOW_CE_ENV, "CUR_S3_BUCKET", "CUR_ATHENA_DATABASE",
              "CUR_ATHENA_TABLE", "CUR_ATHENA_RESULTS_BUCKET",
              "AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_SUBSCRIPTION_ID",
              "GCP_BQ_BILLING_TABLE", "GOOGLE_APPLICATION_CREDENTIALS",
              "GCP_SERVICE_ACCOUNT_KEY_PATH"):
        monkeypatch.delenv(k, raising=False)
    yield


# ── the opt-in fails closed ──────────────────────────────────────────────────

@pytest.mark.parametrize("value", [
    "", " ", "0", "false", "False", "no", "off", "disabled", "null", "None",
    "2", "true;", "1 1", "y", "t", "TRUE!", "\t", "\n", "01", "1.0",
])
def test_every_ambiguous_opt_in_value_means_off(monkeypatch, value):
    """Fail closed. Anything that is not an exact opt-in leaves CE off, so a
    half-configured environment cannot start billing the customer."""
    monkeypatch.setenv(ba.ALLOW_CE_ENV, value)
    assert ba.cost_explorer_allowed() is False, f"{value!r} enabled Cost Explorer"


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "True", "yes", " yes ", "  1  "])
def test_the_documented_opt_in_values_work(monkeypatch, value):
    monkeypatch.setenv(ba.ALLOW_CE_ENV, value)
    assert ba.cost_explorer_allowed() is True


def test_cost_explorer_is_off_with_no_environment_at_all():
    assert ba.cost_explorer_allowed() is False


def test_ce_client_refuses_and_names_the_fix():
    with pytest.raises(ba.BillingAccessError) as e:
        ba.ce_client()
    msg = str(e.value)
    assert "aws-cur-setup.yaml" in msg, "the refusal must name the template to deploy"
    assert ba.ALLOW_CE_ENV in msg, "the refusal must name the escape hatch"
    assert "bills your account per request" in msg


def test_ce_client_refuses_even_when_handed_a_session():
    """A caller passing its own session must not bypass the gate: that is the
    shape of every 'but I already have credentials' workaround."""
    class FakeSession:
        def client(self, *a, **k):
            raise AssertionError("the gate was bypassed via an injected session")

    with pytest.raises(ba.BillingAccessError):
        ba.ce_client(session=FakeSession())


def test_ce_client_builds_only_after_an_explicit_opt_in(monkeypatch):
    monkeypatch.setenv(ba.ALLOW_CE_ENV, "1")
    built = {}

    class FakeSession:
        def client(self, service, **k):
            built.update(service=service, **k)
            return "client"

    assert ba.ce_client(session=FakeSession()) == "client"
    assert built["service"] == "ce"


def test_permitting_cost_explorer_is_logged_loudly(monkeypatch, caplog):
    """It spends the customer's money. It may never happen quietly."""
    monkeypatch.setenv(ba.ALLOW_CE_ENV, "1")

    class FakeSession:
        def client(self, *a, **k):
            return object()

    with caplog.at_level("WARNING"):
        ba.ce_client(session=FakeSession(), reason="unit test")
    assert "billed per request" in caplog.text
    assert ba.ALLOW_CE_ENV in caplog.text


# ── provisioning detection ───────────────────────────────────────────────────

_CUR_KEYS = ("CUR_S3_BUCKET", "CUR_ATHENA_DATABASE", "CUR_ATHENA_TABLE",
             "CUR_ATHENA_RESULTS_BUCKET")


def test_aws_is_not_provisioned_until_every_cur_key_is_set(monkeypatch):
    """Three of four is not configured. A partial setup that reads as ready
    produces a confident wrong answer, which is the worst outcome."""
    for i in range(len(_CUR_KEYS)):
        for k in _CUR_KEYS:
            monkeypatch.delenv(k, raising=False)
        for k in _CUR_KEYS[:i]:
            monkeypatch.setenv(k, "x")
        expected = i == len(_CUR_KEYS)
        assert ba.provisioned("aws") is expected, f"{i} of 4 keys -> {expected}"


def test_a_whitespace_only_value_is_not_configuration(monkeypatch):
    for k in _CUR_KEYS:
        monkeypatch.setenv(k, "   ")
    assert ba.provisioned("aws") is False


def test_gcp_needs_both_the_export_table_and_a_credential(monkeypatch):
    monkeypatch.setenv("GCP_BQ_BILLING_TABLE", "proj.ds.gcp_billing_export_v1")
    assert ba.provisioned("gcp") is False, "an export table with no credential is not access"
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/sa.json")
    assert ba.provisioned("gcp") is True


def test_gcp_credential_alone_is_not_billing_access(monkeypatch):
    """A service account that can list projects cannot read the bill. This is
    exactly the confusion that made GCP look connected while returning nothing."""
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/sa.json")
    assert ba.provisioned("gcp") is False


def test_azure_needs_the_service_principal_triple(monkeypatch):
    monkeypatch.setenv("AZURE_TENANT_ID", "t")
    monkeypatch.setenv("AZURE_CLIENT_ID", "c")
    assert ba.provisioned("azure") is False
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "s")
    assert ba.provisioned("azure") is True


@pytest.mark.parametrize("name", ["AWS", " aws ", "Aws", "aWs"])
def test_provider_names_are_normalised(monkeypatch, name):
    for k in _CUR_KEYS:
        monkeypatch.setenv(k, "x")
    assert ba.provisioned(name) is True


@pytest.mark.parametrize("name", ["", None, "digitalocean", "ce", "cur"])
def test_an_unknown_provider_is_never_reported_as_provisioned(name):
    assert ba.provisioned(name) is False


def test_missing_setup_raises_on_an_unknown_provider():
    """Returning None would make "nothing is missing" and "I do not know what
    this is" the same answer, so a typo'd provider would read as provisioned."""
    with pytest.raises(ValueError) as e:
        ba.missing_setup("awss")
    assert "known:" in str(e.value)


def test_missing_setup_returns_the_path_when_unprovisioned():
    path = ba.missing_setup("aws")
    assert path is not None
    assert "aws-cur-setup.yaml" in path.artifact
    assert set(path.env_keys) == set(_CUR_KEYS)


def test_missing_setup_returns_none_only_when_actually_provisioned(monkeypatch):
    for k in _CUR_KEYS:
        monkeypatch.setenv(k, "x")
    assert ba.missing_setup("aws") is None


# ── the refusal is never mistaken for a finding ──────────────────────────────

def test_unavailable_never_returns_a_zero_or_an_empty_result():
    """A cost tool that returns $0 because it could not read the bill is worse
    than one that refuses: somebody will believe the zero."""
    out = ba.unavailable("aws")
    flat = repr(out)
    assert out["error"] == "billing_export_not_configured"
    assert "total" not in out and "cost" not in out and "spend" not in out
    assert 0 not in [v for v in out.values() if isinstance(v, (int, float))]
    assert "not a finding of zero spend" in flat


def test_unavailable_tells_the_reader_what_still_works():
    out = ba.unavailable("aws")
    assert "nable scan" in out["note"], "refusing must not read as 'nable does nothing'"


def test_unavailable_carries_the_lead_time_honestly():
    """24 hours is not instant and pretending otherwise burns the first
    impression of anyone who deploys the stack and immediately asks a question."""
    for provider in ("aws", "azure", "gcp"):
        assert "24 hours" in ba.unavailable(provider)["lead_time"]


def test_require_is_truthy_when_it_blocks_and_none_when_it_passes(monkeypatch):
    """Used as `if err := require(...): return err`. An empty dict would be
    falsy and silently let an unprovisioned call through."""
    blocked = ba.require("aws")
    assert blocked, "require() must return a truthy payload when it blocks"
    assert isinstance(blocked, dict) and len(blocked) > 3

    for k in _CUR_KEYS:
        monkeypatch.setenv(k, "x")
    assert ba.require("aws") is None


def test_every_provider_that_can_be_probed_has_a_documented_setup_path():
    """A provider `provisioned()` knows about but ACCESS_PATHS does not would
    produce a refusal with no instructions."""
    for provider in (ba.AWS, ba.AZURE, ba.GCP):
        assert provider in ba.ACCESS_PATHS
        path = ba.ACCESS_PATHS[provider]
        assert path.env_keys and path.mechanism and path.artifact
        assert len(path.as_instructions()) == 3


def test_the_aws_setup_path_points_at_a_template_that_exists():
    """A refusal that names a file we do not ship is worse than no refusal."""
    repo = PKG.parent.parent          # src/finops -> src -> repo root
    template = repo / ba.ACCESS_PATHS[ba.AWS].artifact
    assert template.exists(), f"{template} is referenced in every AWS refusal"
    body = template.read_text()
    assert "AWS::CUR::ReportDefinition" in body
    assert "AWS::Glue::Database" in body


# ── the ratchet: direct Cost Explorer clients may only shrink ────────────────

# Every module that still builds its own Cost Explorer client. This list may
# SHRINK as call sites move to billing_access.ce_client(). It may never grow: a
# new entry means a new way to bill the customer that bypasses the gate.
LEGACY_CE_SITES: frozenset[str] = frozenset({
    "anomaly/backfill.py", "anomaly/detector.py", "attribution/fetcher.py",
    "cli_scan.py", "connectors/aws.py", "connectors/aws_org.py",
    "connectors/aws_services/bedrock.py", "connectors/aws_services/documentdb.py",
    "connectors/aws_services/marketplace.py", "connectors/aws_services/textract.py",
    "connectors/kubernetes_costs.py", "connectors/llm_costs.py",
    "connectors/universal.py", "doctor.py", "ml/forecasting.py",
    "recommendations/bedrock_routing.py", "recommendations/commitments.py",
    "recommendations/database_savings_plans.py", "recommendations/genuine_savings.py",
    "recommendations/rate_detector.py", "recommendations/textract_env.py",
    "security/iam_setup.py", "setup_wizard.py", "tools/aws.py",
})


def _ce_client_sites() -> set[str]:
    """Every module constructing a Cost Explorer client, found structurally.

    AST rather than grep: `boto3.client("ce", ...)`, `session.client('ce')` and
    a client built from a variable all read the same to a regex, and a regex
    over source would also match the string "ce" in a comment.
    """
    found: set[str] = set()
    for path in sorted(PKG.rglob("*.py")):
        rel = str(path.relative_to(PKG))
        if rel == "billing_access.py":
            continue                       # the one permitted construction
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:                # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "client" or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and first.value == "ce":
                found.add(rel)
    return found


def test_no_new_module_may_build_its_own_cost_explorer_client():
    """The ratchet. A module not on the legacy list that constructs a CE client
    is a new, ungated way to bill the customer."""
    new = _ce_client_sites() - LEGACY_CE_SITES
    assert not new, (
        "These build a Cost Explorer client without going through "
        "billing_access.ce_client():\n  " + "\n  ".join(sorted(new))
    )


def test_the_legacy_list_may_not_contain_modules_that_are_already_clean():
    """Forces the list down. A migrated module left on the list makes the
    remaining work look larger than it is and the ratchet stops meaning anything."""
    stale = LEGACY_CE_SITES - _ce_client_sites()
    assert not stale, (
        "These no longer build a Cost Explorer client; remove them from "
        "LEGACY_CE_SITES:\n  " + "\n  ".join(sorted(stale))
    )


def test_the_scanner_actually_detects_a_ce_client(tmp_path):
    """Mutation check on the ratchet itself. A scanner that finds nothing would
    make both tests above pass forever."""
    import textwrap

    src = textwrap.dedent("""
        import boto3
        def f():
            return boto3.client("ce", region_name="us-east-1")
    """)
    tree = ast.parse(src)
    hits = [n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "client" and n.args
            and isinstance(n.args[0], ast.Constant) and n.args[0].value == "ce"]
    assert len(hits) == 1

    # ...and does not fire on a different service or a bare mention of "ce".
    other = ast.parse('import boto3\nx = boto3.client("ec2")\ny = "ce"\n')
    hits2 = [n for n in ast.walk(other)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "client" and n.args
             and isinstance(n.args[0], ast.Constant) and n.args[0].value == "ce"]
    assert hits2 == []


def test_the_ratchet_covers_the_whole_package_not_a_sample():
    """If rglob ever stopped seeing most of the tree, both ratchet tests would
    pass vacuously."""
    assert len(list(PKG.rglob("*.py"))) > 100
    assert _ce_client_sites(), "the scanner found nothing at all; it is broken"


# ── the suite itself may not spend money ─────────────────────────────────────

def test_the_session_guard_blocks_a_real_cost_explorer_call():
    """Meta-test on the conftest fixture. If it silently stops working, the
    suite goes back to making billed ce:GetCostAndUsage requests against
    whoever's credentials happen to be in the environment, which is exactly what
    it was doing before this existed."""
    import boto3

    client = boto3.client("ce", region_name="us-east-1",
                          aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
                          aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
    with pytest.raises(AssertionError) as e:
        client.get_cost_and_usage(
            TimePeriod={"Start": "2026-08-01", "End": "2026-08-02"},
            Granularity="MONTHLY", Metrics=["UnblendedCost"])
    assert "spends real money" in str(e.value)


def test_the_session_guard_blocks_athena_too():
    """Athena bills per byte scanned. The CUR path is cheaper than Cost
    Explorer, not free, so a test must not scan a real bucket either."""
    import boto3

    client = boto3.client("athena", region_name="us-east-1",
                          aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
                          aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
    with pytest.raises(AssertionError):
        client.start_query_execution(QueryString="SELECT 1")


def test_the_guard_does_not_block_unbilled_services():
    """A guard that blocks everything would be turned off within a week.
    describe calls are free and must still reach botocore's own error path."""
    import boto3
    import botocore.exceptions

    client = boto3.client("ec2", region_name="us-east-1",
                          aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
                          aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
    # Reaches the network layer and fails there (or on auth), NOT on our guard.
    with pytest.raises(Exception) as e:
        client.describe_volumes(VolumeIds=["vol-00000000000000000"])
    assert "spends real money" not in str(e.value)
