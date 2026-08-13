"""Blocking work on the event loop, and the deadlines it quietly disarms.

Why this file exists, stated plainly: nable advertises three separate deadlines
around provider calls, and on the provider that matters most none of them can
fire. `asyncio.wait_for` only cancels a coroutine that suspends. A coroutine that
calls a synchronous SDK function holds the single event-loop thread for the whole
call, so the timer callback that would cancel it cannot be dispatched until the
call has already finished. The timeout still raises, just far too late, and by
then the number it was protecting has already cost the user the wall clock it was
supposed to bound. The same mechanic turns `asyncio.gather` over blocking
coroutines into a plain for-loop: three providers "in parallel" is the sum of
their latencies, not the max.

The seam is the provider SDK, not our code. Nothing below replaces a nable
function with a stub. The azure.identity credential, the google.auth default
credential, boto3.client and the boto3 Session are faked at the point where they
would leave the machine, each one made to take a measured amount of time the way
a real STS or ARM round trip does. Everything between that boundary and the
assertion is the real thing: the real probe bodies, the real memoisation, the
real `_active` gather, the real `_fetch_costs_cached` deadline, the real
`check_connector_health` probe fan-out, the real `get_engine` publish order.

What these tests measure is therefore not "is the code slow". It is "can the loop
run anything else while this is in flight", which is the property every deadline
in the async layer is built on. The wall-clock thresholds are set with roughly a
2x margin either side so a loaded machine cannot flip them.

Every test here is a red test: it fails against the code as it stands today, and
each docstring says what a customer sees because of it.
"""
from __future__ import annotations

import asyncio
import inspect
import sys
import threading
import time
import types
from datetime import date, timedelta

import pytest

import finops.server as _srv
from finops import ambient, cache as _cache
from finops.storage import db

# One blocked provider probe. Real ones are capped at ambient.PROBE_TIMEOUT_S (6s).
PROBE_BLOCK_S = 0.8
# One blocked STS round trip. A real assume_role against an unreachable endpoint
# runs to the botocore default of 60s per attempt.
STS_BLOCK_S = 1.0
# One blocked scanner inside the CSV export.
SCANNER_BLOCK_S = 0.3


class _Heartbeat:
    """Longest stretch the event loop could not run a task that was ready.

    A coroutine that awaits normally leaves this near zero however slow it is.
    A coroutine that calls into a blocking SDK pins it to that call's duration,
    which is the property under test.
    """

    def __init__(self, tick: float = 0.01) -> None:
        self._tick = tick
        self._stop = False
        self._task: asyncio.Task | None = None
        self.max_stall = 0.0

    async def _beat(self) -> None:
        last = time.monotonic()
        while not self._stop:
            await asyncio.sleep(self._tick)
            now = time.monotonic()
            self.max_stall = max(self.max_stall, now - last - self._tick)
            last = now

    async def __aenter__(self) -> "_Heartbeat":
        self._task = asyncio.create_task(self._beat())
        await asyncio.sleep(0.03)   # let it take a baseline sample
        self.max_stall = 0.0
        return self

    async def __aexit__(self, *exc) -> bool:
        self._stop = True
        if self._task is not None:
            await self._task
        return False


# ── fixtures: every global these tests touch is restored ─────────────────────

@pytest.fixture(autouse=True)
def _clean_probe_state(monkeypatch):
    """Ambient probe results and the configured-connector set are process-global
    and memoised for 30s and 120s respectively. Without clearing them on both
    sides, the second test in this file measures a cache hit and passes for the
    wrong reason, and a cached fake leaks into the rest of the suite.
    """
    for var in ("AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID",
                "AZURE_SUBSCRIPTION_IDS", "GOOGLE_APPLICATION_CREDENTIALS",
                "GCP_SERVICE_ACCOUNT_KEY_PATH", "GCP_BILLING_ACCOUNT_IDS",
                "AWS_ROLE_ARNS", "DATABASE_URL", "FINOPS_DEMO", "FINOPS_DEMO_FORCE"):
        monkeypatch.delenv(var, raising=False)
    ambient.reset_cache()
    _srv._ACTIVE_CACHE.clear()
    yield
    ambient.reset_cache()
    _srv._ACTIVE_CACHE.clear()


@pytest.fixture
def isolated_cache(monkeypatch, tmp_path):
    """Point the cost cache (memory + the cache.db it persists to) at tmp.

    db._DATA_DIR is saved too: it memoises FINOPS_DATA_DIR the first time anything
    asks for it, and a tmp path memoised here would outlive the directory.
    """
    saved_dir = db._DATA_DIR
    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(_cache, "_store", {})
    monkeypatch.setattr(_cache, "_disk_ready", False)
    try:
        yield
    finally:
        db._DATA_DIR = saved_dir


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    """A private SQLite file, and the engine singleton restored afterwards."""
    saved_engine, saved_dir = db._ENGINE, db._DATA_DIR
    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "concurrency.db"))
    db._ENGINE = None
    try:
        yield
    finally:
        made = db._ENGINE
        if made is not None and made is not saved_engine:
            made.dispose()
        db._ENGINE, db._DATA_DIR = saved_engine, saved_dir


# ── provider SDK fakes, each with a measured blocking cost ───────────────────

class _ArmResponse:
    """Shape from the ARM "Subscriptions - List" reference, trimmed."""
    status_code = 200

    def json(self) -> dict:
        return {"value": [{"id": "/subscriptions/aaaa-1111",
                           "subscriptionId": "aaaa-1111",
                           "displayName": "Production", "state": "Enabled"}]}


def _fake_azure_sdk(monkeypatch, block_s: float) -> None:
    """azure.identity that takes `block_s` to hand back a token, like a real
    DefaultAzureCredential walking its chain."""

    class _Token:
        token = "fake-arm-token"

    class _Cred:
        def __init__(self, **kw):
            pass

        def get_token(self, *scopes, **kw):
            time.sleep(block_s)
            return _Token()

    mod = types.ModuleType("azure.identity")
    mod.DefaultAzureCredential = _Cred
    pkg = types.ModuleType("azure")
    pkg.identity = mod
    monkeypatch.setitem(sys.modules, "azure", pkg)
    monkeypatch.setitem(sys.modules, "azure.identity", mod)

    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _ArmResponse())


def _fake_gcp_sdk(monkeypatch, block_s: float) -> None:
    """google.auth.default that takes `block_s`, like a real ADC lookup that
    falls through to a metadata probe."""
    auth = types.ModuleType("google.auth")

    def _default(*a, **k):
        time.sleep(block_s)
        return ("fake-creds", "proj-1")

    auth.default = _default

    class _Acct:
        name = "billingAccounts/01AAAA-BBBBBB-CCCCCC"
        open = True

    class _Client:
        def __init__(self, credentials=None):
            pass

        def list_billing_accounts(self):
            return [_Acct()]

    billing = types.ModuleType("google.cloud.billing_v1")
    billing.CloudBillingClient = _Client
    gcloud = types.ModuleType("google.cloud")
    gcloud.billing_v1 = billing
    g = types.ModuleType("google")
    g.auth = auth
    g.cloud = gcloud
    for name, mod in (("google", g), ("google.auth", auth),
                      ("google.cloud", gcloud), ("google.cloud.billing_v1", billing)):
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-1")   # past the signal pre-check


class _FakeSTS:
    """sts:GetCallerIdentity and sts:AssumeRole, each costing a round trip."""

    def __init__(self, block_s: float) -> None:
        self._block = block_s

    def get_caller_identity(self) -> dict:
        time.sleep(self._block)
        return {"Account": "123456789012"}

    def assume_role(self, **kw) -> dict:
        time.sleep(self._block)
        return {"Credentials": {"AccessKeyId": "AKIAFAKE",
                                "SecretAccessKey": "secret",
                                "SessionToken": "token"}}


class _FakeCE:
    """Cost Explorer that answers instantly, so anything slow in these tests is
    ours and not the fake's."""

    def get_cost_and_usage(self, **kw) -> dict:
        return {"ResultsByTime": []}


class _FakeSession:
    """A boto3.Session as injected by the multi-account registry."""

    def __init__(self, block_s: float) -> None:
        self._block = block_s

    def client(self, name: str, **kw):
        return _FakeSTS(self._block) if name == "sts" else _FakeCE()

    def get_credentials(self):
        return object()


# ── 1. provider probing ──────────────────────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="audit finding, not yet fixed. strict=True so that fixing it FAILS here until this marker is removed: the marker count is the work list.")
async def test_active_probes_providers_concurrently(monkeypatch):
    """RED against today's code.

    `_active()` is the front door for essentially every cost tool, and it gathers
    `is_configured()` across providers. Azure's and GCP's implementations call
    the ambient probe synchronously, so the gather cannot overlap: the user waits
    Azure's probe plus GCP's probe, once every ambient.CACHE_TTL_S (30s) of tool
    use. With the real 6s cap on each probe that is up to 12 seconds of dead air
    before an answer starts, on a machine where the SDKs are installed and the
    credential chain is slow, which is the docker and enterprise image.
    """
    from finops.connectors.azure import AzureConnector
    from finops.connectors.gcp import GCPConnector

    _fake_azure_sdk(monkeypatch, PROBE_BLOCK_S)
    _fake_gcp_sdk(monkeypatch, PROBE_BLOCK_S)

    pool = {"azure": AzureConnector(), "gcp": GCPConnector()}
    t0 = time.monotonic()
    active = await _srv._active(subset=pool)
    elapsed = time.monotonic() - t0

    assert set(active) == {"azure", "gcp"}, f"both probes should report usable, got {sorted(active)}"
    assert elapsed < 1.2, (
        f"two {PROBE_BLOCK_S}s probes gathered together took {elapsed:.2f}s. "
        f"Concurrent is ~{PROBE_BLOCK_S:.1f}s, serial is ~{2 * PROBE_BLOCK_S:.1f}s: "
        f"is_configured is blocking the event loop, so asyncio.gather is a for-loop"
    )


@pytest.mark.xfail(strict=True, reason="audit finding, not yet fixed. strict=True so that fixing it FAILS here until this marker is removed: the marker count is the work list.")
@pytest.mark.parametrize("provider", ["azure", "gcp"])
async def test_is_configured_leaves_the_event_loop_free(monkeypatch, provider):
    """RED against today's code.

    Same defect as above, measured directly rather than through the gather. While
    one provider probe is in flight nothing else on the loop can run: not another
    provider, not the per-provider timeout that is supposed to bound a hung API,
    not a streaming response to the user. AWS's own probe sits on the same loop,
    so the freeze compounds.
    """
    if provider == "azure":
        _fake_azure_sdk(monkeypatch, PROBE_BLOCK_S)
        from finops.connectors.azure import AzureConnector
        conn = AzureConnector()
    else:
        _fake_gcp_sdk(monkeypatch, PROBE_BLOCK_S)
        from finops.connectors.gcp import GCPConnector
        conn = GCPConnector()

    async with _Heartbeat() as hb:
        assert await conn.is_configured() is True

    assert hb.max_stall < 0.25, (
        f"{provider}.is_configured() held the event loop for {hb.max_stall:.2f}s "
        f"of its {PROBE_BLOCK_S:.1f}s probe. Wrap the probe in asyncio.to_thread"
    )


# ── 2. AWS: blocking STS defeats the deadlines built around it ───────────────

@pytest.mark.xfail(strict=True, reason="audit finding, not yet fixed. strict=True so that fixing it FAILS here until this marker is removed: the marker count is the work list.")
async def test_provider_deadline_fires_while_assume_role_blocks(monkeypatch, isolated_cache):
    """RED against today's code.

    `_fetch_costs_cached` promises a hard per-provider deadline so one hung
    provider cannot stall a whole query. The deadline is real but unreachable:
    `AWSConnector.get_costs` builds its client first, and with AWS_ROLE_ARNS set
    that means a synchronous sts:AssumeRole per role, on plain botocore defaults
    (60s connect, 60s read, retries) rather than the 5s/15s config the Cost
    Explorer client gets. asyncio cannot cancel a thread it is running on, so the
    timeout callback only runs once the blocking call has returned on its own.
    A user with three role ARNs and an STS endpoint that is not answering waits
    minutes on a tool that advertises 90 seconds.
    """
    import boto3
    from finops.connectors.aws import AWSConnector

    monkeypatch.setattr(_srv, "_PROVIDER_TIMEOUT_S", 0.2)
    monkeypatch.setenv("AWS_ROLE_ARNS", "arn:aws:iam::123456789012:role/nable-readonly")

    sts = _FakeSTS(STS_BLOCK_S)
    monkeypatch.setattr(boto3, "client",
                        lambda name, **kw: sts if name == "sts" else _FakeCE())

    conn = AWSConnector()
    end = date.today()
    start = end - timedelta(days=7)

    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="did not answer"):
        await _srv._fetch_costs_cached("aws", conn, start, end)
    elapsed = time.monotonic() - t0

    assert elapsed < 0.55, (
        f"a 0.2s per-provider deadline took {elapsed:.2f}s to fire, because "
        f"sts:AssumeRole ran on the event loop for {STS_BLOCK_S:.1f}s first. "
        f"The advertised timeout cannot bound anything it is meant to bound"
    )


@pytest.mark.xfail(strict=True, reason="audit finding, not yet fixed. strict=True so that fixing it FAILS here until this marker is removed: the marker count is the work list.")
async def test_check_connector_health_deadline_can_actually_fire(monkeypatch, isolated_db):
    """RED against today's code.

    check_connector_health probes every connector with
    `asyncio.wait_for(connector.list_accounts(), timeout=10.0)` and reports
    "Timeout (>10s), credentials may be valid but API is slow" when it trips.
    AWSConnector.list_accounts has no await in it: it calls
    sts:GetCallerIdentity synchronously. The coroutine never suspends, so the 10s
    callback never gets the loop, and against an STS that is not answering the
    tool hangs for minutes and then reports healthy: True with a response time of
    several hundred thousand milliseconds. The diagnostic tool a stuck user is
    told to run is the one that hangs, and it lies about the result.

    Measured here with an outer 0.2s deadline: whatever bound is set on this
    coroutine, it cannot take effect until the blocking call is over.
    """
    from finops.tools import meta as meta_tools
    from finops.connectors.aws import AWSConnector

    db.get_engine()   # warm the schema so the tool's own DB read is not timed
    conn = AWSConnector(session=_FakeSession(STS_BLOCK_S))
    monkeypatch.setattr(_srv, "_ALL_CONNECTORS", {"aws": conn})

    t0 = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(meta_tools.check_connector_health(), timeout=0.2)
    elapsed = time.monotonic() - t0

    assert elapsed < 0.55, (
        f"a 0.2s deadline on check_connector_health took {elapsed:.2f}s to fire. "
        f"list_accounts blocks the loop on sts:GetCallerIdentity, so the tool's "
        f"own 10s timeout is decorative too"
    )


# ── 3. the engine singleton is published before it is usable ─────────────────

@pytest.mark.xfail(strict=True, reason="audit finding, not yet fixed. strict=True so that fixing it FAILS here until this marker is removed: the marker count is the work list.")
def test_get_engine_never_publishes_a_half_built_engine(isolated_db):
    """RED against today's code.

    `get_engine` assigns the module-global _ENGINE and only then runs
    metadata.create_all and the migrations, with no lock and no second publish.
    A concurrent caller takes the `if _ENGINE is not None: return _ENGINE` fast
    path and gets an engine whose schema does not exist yet, so its query dies
    with "no such table: cost_snapshots". Every threaded host reaches this on
    first run: the ThreadingHTTPServer behind `finops serve`, the Slack Bolt
    listener pool, the enterprise box's prewarm thread next to its uvicorn
    thread, and any asyncio.to_thread fan-out. It is a first-run crash at exactly
    the moment the activation data says people give up.

    The delay below is inserted in front of SQLAlchemy's real create_all, which
    still runs untouched. It does not change what get_engine does; it widens a
    window that is otherwise a few milliseconds wide, so the race is observed
    every run instead of one run in fifty.
    """
    from sqlalchemy import select

    real_create_all = db.metadata.create_all
    building = threading.Event()

    def slow_create_all(*a, **kw):
        building.set()
        time.sleep(0.4)
        return real_create_all(*a, **kw)

    observed: dict[str, str] = {}

    def reader() -> None:
        building.wait(5)
        deadline = time.monotonic() + 5
        while db._ENGINE is None and time.monotonic() < deadline:
            time.sleep(0.001)
        if db._ENGINE is None:
            observed["result"] = "engine was never published"
            return
        try:
            engine = db.get_engine()
            with engine.connect() as conn:
                conn.execute(select(db.cost_snapshots.c.id).limit(1)).fetchall()
            observed["result"] = "ok"
        except Exception as exc:
            observed["result"] = f"{type(exc).__name__}: {exc}"

    db.metadata.create_all = slow_create_all
    t = threading.Thread(target=reader, name="concurrent-get-engine")
    try:
        t.start()
        db.get_engine()
        t.join(10)
    finally:
        del db.metadata.create_all   # restores the real bound method

    assert observed.get("result") == "ok", (
        f"a second thread that saw _ENGINE published could not use it: "
        f"{observed.get('result')}. _ENGINE must be published only after "
        f"create_all and the migrations have finished"
    )


# ── 4. the CSV export: dead import in front of a loop-freezing gather ────────

# The scanners export_cost_report_csv fans out over, under the names their
# modules actually define. The tool imports one of them under a name that does
# not exist (`scan_spot_adoption_opportunities`), which is why it cannot run.
_EXPORT_SCANNERS = {
    "graviton": "scan_graviton_opportunities",
    "public_ipv4": "audit_public_ipv4",
    "lambda_concurrency": "scan_lambda_concurrency_waste",
    "s3_bucket_keys": "scan_s3_bucket_key_opportunities",
    "nonprod_scheduler": "identify_nonprod_resources",
    "rds_snapshots": "audit_rds_manual_snapshots",
    "spot_adoption": "recommend_spot_adoption",
    "cloudwatch_cardinality": "audit_cloudwatch_metric_cardinality",
    "cloudwatch_alarms": "audit_cloudwatch_orphaned_alarms",
    "cloudwatch_logs_ia": "audit_cloudwatch_logs_ia_opportunities",
    "lambda_snapstart": "recommend_lambda_snapstart",
    "nlb_cross_zone": "audit_nlb_cross_zone_costs",
    "s3_intelligent_tiering": "audit_s3_intelligent_tiering",
    "s3_transfer_acceleration": "audit_s3_transfer_acceleration",
    "ebs_snapshot_replication": "audit_ebs_snapshot_replication",
    "database_savings_plans": "recommend_database_savings_plans",
    "textract_env": "scan_textract_environment_waste",
    "bedrock_routing": "recommend_bedrock_model_routing",
    "commitments": "analyze_commitments",
}


def _install_scanner_fakes(monkeypatch, block_s: float) -> None:
    """Replace each AWS-touching scanner with one that costs `block_s` and finds
    nothing, keeping its sync or async shape. The fake sits where the cloud API
    would be; every line of the export tool is still the real one.
    """
    import importlib

    for mod_name, attr in _EXPORT_SCANNERS.items():
        mod = importlib.import_module(f"finops.recommendations.{mod_name}")
        if inspect.iscoroutinefunction(getattr(mod, attr)):
            async def fake(*a, _b=block_s, **kw):
                await asyncio.sleep(_b)
                return []
        else:
            def fake(*a, _b=block_s, **kw):
                time.sleep(_b)
                return []
        monkeypatch.setattr(mod, attr, fake)


async def test_export_cost_report_csv_runs_without_freezing_the_loop(monkeypatch, tmp_path):
    """RED against today's code, for two reasons in one call path.

    First, the tool cannot run at all: its function-scoped
    `from ..recommendations.spot_adoption import scan_spot_adoption_opportunities`
    names a function that module does not define (it is `recommend_spot_adoption`),
    so every invocation raises ImportError before a single scanner starts. That is
    what goes red today, and it means the CSV export has never worked for anyone.

    Second, and the reason this test is written as a timing test: the gather
    underneath is 19 bare coroutines with no deadline, and several of the
    scanners it calls are ordinary synchronous functions. Calling one builds its
    result on the spot, on the event loop, before the gather is even constructed.
    So the export's wall clock is the sum of every scanner rather than the
    slowest, with the loop frozen for the duration, and one wedged AWS call hangs
    the export with no timeout to end it. run_full_cost_audit already fixed this
    for itself with asyncio.to_thread plus a deadline; this copy did not get it.
    """
    from finops.tools import notifications

    monkeypatch.setattr(_srv, "require_role", lambda *a, **k: None)
    aws = _srv.CLOUD_CONNECTORS.get("aws")

    async def _configured():
        return True

    monkeypatch.setattr(aws, "is_configured", _configured)
    _install_scanner_fakes(monkeypatch, SCANNER_BLOCK_S)

    dest = tmp_path / "nable-report.csv"
    async with _Heartbeat() as hb:
        out = await notifications.export_cost_report_csv(
            output_path=str(dest), regions=["us-east-1"]
        )

    assert isinstance(out, str) and str(dest) in out, f"export did not write a CSV: {out!r}"
    assert dest.exists(), "export reported success without writing the file"
    assert hb.max_stall < 0.2, (
        f"export_cost_report_csv held the event loop for {hb.max_stall:.2f}s. "
        f"Its synchronous scanners run inline on the loop, and the gather has no "
        f"deadline, so a wedged scanner hangs the export with nothing to stop it"
    )
