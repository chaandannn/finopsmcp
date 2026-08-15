"""Regressions for six defects on the MCP tool surface: what nable advertises,
what a tool hands back to the model, and what a tool call is allowed to touch.

Why this file exists, stated plainly: the tool surface is the only part of nable
a model ever sees. Everything the product promises about itself rides on three
declarations that are made in one place and honoured somewhere else entirely:
the readOnlyHint annotation in tool_surface.py, the capability map in
capabilities.py, and the `-> str` return annotation FastMCP turns into an output
schema. Nothing checks that any of them matches the code they describe. Six of
them do not.

The seam is deliberate. Every test below drives the REAL dispatch path,
`server.mcp.call_tool(name, args)`, the same entry FastMCP uses for a tools/call
over stdio, so the wrapper, the output-schema validation and the annotation are
all live. Nothing replaces a nable function. The fakes sit at the three real
boundaries a tool call crosses:

  - the process boundary: a `terraform` shim on disk that records its argv,
    so the subprocess a "read-only" tool spawns is observable without a real
    terraform, a real backend, or a real state lock;
  - the credential boundary: a tripwire connector whose get_costs raises, so
    "did this reach an account that could be billed" is a fact and not an
    inference;
  - the provider boundary: a stand-in finops.server_web, which is the private
    half of the open-core split and is genuinely absent from this repo.

What these tests still cannot prove: that a real Anthropic client renders a
ToolError the way the transcript in the finding did, or that a real terraform
takes the DynamoDB lock. Those need a live client and a live backend. What they
do prove is that nable emits the shape and runs the command that leads there.

Every test here is the FAILS-NOW kind, red against current behaviour because
the defect is real and green once it is fixed, with one exception:
test_the_demo_chokepoint_control_passes_for_a_tool_that_gets_it_right passes
today. It is the control. It pins the one cost tool that does branch on demo
mode, which both proves the tripwire fixture can go green and stops that branch
being deleted.
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
import json
import pkgutil
import textwrap
import types

import pytest

from finops import server


# ── shared helpers ────────────────────────────────────────────────────────────

def _call(name: str, args: dict | None = None):
    """Dispatch a tool the way a real MCP client does."""
    return asyncio.run(server.mcp.call_tool(name, args or {}))


def _text(result) -> str:
    """Flatten whatever call_tool returned into the text the model would read.

    convert_result hands back either a list of content blocks or a
    (unstructured, structured) pair depending on the tool's output schema, so
    walk both shapes rather than guessing.
    """
    parts: list[str] = []

    def walk(node):
        if isinstance(node, (list, tuple)):
            for item in node:
                walk(item)
        elif hasattr(node, "text"):
            parts.append(str(node.text))
        elif isinstance(node, dict) or node is None:
            parts.append(json.dumps(node, default=str))
        else:
            parts.append(str(node))

    walk(result)
    return "\n".join(parts)


def _registry() -> dict:
    return dict(server.mcp._tool_manager._tools)


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    """A private SQLite file, so a tool that writes (create_api_key) or reads
    (the policy gate's ledger) never touches the developer's ~/.finops/finops.db.
    The engine is a module global, so it is torn down on both sides."""
    import finops.storage.db as db_mod

    monkeypatch.setenv("FINOPS_DB_PATH", str(tmp_path / "surface.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    db_mod._ENGINE = None
    yield db_mod
    db_mod._ENGINE = None


@pytest.fixture(autouse=True)
def no_leaked_identity():
    """require_role reads a ContextVar. A test that attaches a viewer must not
    leave it attached for the next one."""
    from finops.auth.rbac import current_identity, set_current_identity

    before = current_identity()
    yield
    set_current_identity(before)


def _viewer():
    from finops.auth.rbac import Identity

    return Identity(key_id=1, name="bob@corp.com", email="bob@corp.com",
                    role="viewer", scope_team=None, scope_provider=None)


# ══════════════════════════════════════════════════════════════════════════════
# 1. A denied guard returns a dict from a tool annotated `-> str`
# ══════════════════════════════════════════════════════════════════════════════
# mcp 1.27.2 builds an output schema from the return annotation and validates
# every result against it. `if err := require_role("analyst"): return err`
# returns a dict out of a function declared `-> str`, so the denial never
# reaches the user: FastMCP raises ToolError on the way out.

@pytest.mark.parametrize("tool", ["run_full_cost_audit", "export_cost_report_csv"])
def test_a_denied_tool_answers_the_user_instead_of_crashing_dispatch(isolated_db, tool):
    """FAILS NOW, the bug is real.

    In production a viewer in shared-Postgres mode who asks for a cost audit
    should read "this needs the analyst role, ask an admin". Instead FastMCP
    rejects the guard dict against the tool's own `-> str` output schema and
    raises ToolError, so the model surfaces a pydantic traceback. The same
    happens to a free user who trips require_pro: a crash where the upgrade
    nudge should be. run_full_cost_audit is TIER1, advertised on every message.

    Only the two default-registered offenders are dispatched here. The other
    five live behind FINOPS_ALL_TOOLS and are covered by the sweep below.
    """
    from finops.auth.rbac import set_current_identity

    set_current_identity(_viewer())
    result = _call(tool)          # must not raise
    body = _text(result)
    assert "insufficient_role" in body or "analyst" in body, body[:400]


def _guard_return_samples(fn, guard_dict):
    """Every value a `require_role`/`require_pro` guard in fn actually returns.

    Matched structurally rather than by grepping the source, so the three tools
    that correctly do `return str(err)` are not miscounted as offenders.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.NamedExpr):
            continue
        call = node.test.value
        if not isinstance(call, ast.Call):
            continue
        fname = (call.func.attr if isinstance(call.func, ast.Attribute)
                 else getattr(call.func, "id", ""))
        if fname not in ("require_role", "require_pro"):
            continue
        if len(node.body) != 1 or not isinstance(node.body[0], ast.Return):
            continue
        target, value = node.test.target.id, node.body[0].value
        if isinstance(value, ast.Name) and value.id == target:
            out.append(guard_dict)                       # returns the dict
        elif (isinstance(value, ast.List) and len(value.elts) == 1
              and isinstance(value.elts[0], ast.Name) and value.elts[0].id == target):
            out.append([guard_dict])                     # returns [the dict]
    return out


def _gated_extras():
    """The _EXTRA_TOOLS: hidden from the registry unless FINOPS_ALL_TOOLS=1, but
    importable and callable by name in every install, so their output schema
    matters just as much."""
    import finops.tools as tools_pkg

    found: dict[str, object] = {}
    for mod_info in pkgutil.iter_modules(tools_pkg.__path__):
        mod = importlib.import_module(f"finops.tools.{mod_info.name}")
        for name in server._EXTRA_TOOLS:
            fn = getattr(mod, name, None)
            if callable(fn) and getattr(fn, "__module__", "") == mod.__name__:
                found[name] = inspect.unwrap(fn)
    return found


def test_no_tool_returns_a_guard_shape_its_own_output_schema_rejects():
    """FAILS NOW, the bug is real.

    The registry-wide version of the test above, so a seventh tool cannot join
    the list unnoticed. Every RBAC or licence guard in the codebase returns a
    dict; a tool declared `-> str` cannot emit one. Where the two disagree, the
    denial path is dead and the user gets a validation traceback instead of an
    explanation.
    """
    from mcp.server.fastmcp.tools.base import Tool

    from finops.auth.rbac import require_role

    guard = require_role("analyst", ident=_viewer())
    assert isinstance(guard, dict), "expected a viewer to be denied the analyst role"

    candidates: list[tuple[str, object, object]] = []
    for name, tool in _registry().items():
        candidates.append((name, inspect.unwrap(tool.fn), tool.fn_metadata))
    for name, fn in _gated_extras().items():
        candidates.append((name, fn, Tool.from_function(fn, name=name).fn_metadata))

    offenders = []
    for name, fn, meta in candidates:
        try:
            samples = _guard_return_samples(fn, guard)
        except (OSError, SyntaxError, TypeError):
            continue
        for sample in samples:
            try:
                meta.convert_result(sample)
            except Exception as exc:
                offenders.append(f"{name} ({type(exc).__name__})")
                break

    assert not offenders, (
        "these tools return a guard dict their return annotation rejects, so the "
        "denial path raises ToolError instead of answering: " + ", ".join(sorted(offenders))
    )


# ══════════════════════════════════════════════════════════════════════════════
# 2. The capability map names tools the registry does not hold
# ══════════════════════════════════════════════════════════════════════════════

def test_the_capability_map_only_names_tools_the_registry_holds():
    """FAILS NOW, the bug is real.

    The server instructions tell the model this map is authoritative: "Never
    tell a user a capability is missing because its tool is not in this list."
    So the model reads what_can_nable_do(detailed=True), sees
    audit_public_ipv4_addresses, calls it, and gets "Unknown tool". The share
    group is worse: its gate is `lambda c: True`, so publish_cost_report_to_notion
    and push_to_n8n are offered to every user and neither has an alternate route.
    """
    from finops.capabilities import CATALOG

    registered = set(_registry())
    named = {t for group in CATALOG for t in group["tools"]}
    missing = sorted(named - registered)
    assert not missing, (
        "capabilities.CATALOG advertises tools that are not registered, so the "
        f"model will call them and get 'Unknown tool': {missing}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 3. Three read-only-annotated tools shell out to `terraform plan`
# ══════════════════════════════════════════════════════════════════════════════
# The fake sits at the process boundary: a real executable on PATH that records
# its argv and answers `show -json` with an empty plan. Everything between the
# tool argument and that argv is nable's real code.

_TF_TOOLS = [
    ("estimate_terraform_cost", {}),
    ("estimate_change_cost", {}),
    ("check_action_policy", {"action_type": "terraform_apply"}),
]


@pytest.fixture
def fake_terraform(tmp_path, monkeypatch):
    """A terraform on disk that logs its argv, drops the plan file terraform
    drops, and returns an empty plan from `show -json`."""
    repo = tmp_path / "customer-repo"
    repo.mkdir()
    argv_log = tmp_path / "argv.log"
    argv_log.write_text("")

    shim = tmp_path / "terraform"
    # The plan file goes wherever -out= says, which is what real terraform does.
    # This shim used to write `.plan.tmp` into the cwd unconditionally, ignoring
    # -out entirely. That happened to be right while the code passed
    # `-out=.plan.tmp` with cwd inside the customer's repo, and it made the test
    # unfixable once the code started writing to a temp dir: the assertion was
    # measuring the stub's hardcoded filename, not the argument under test.
    #
    # Modelling -out properly keeps the original defect detectable. Put
    # `-out=.plan.tmp` back in estimate_from_dir and this goes red again, which
    # was checked rather than assumed.
    shim.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{argv_log}"\n'
        'if [ "$1" = "plan" ]; then\n'
        '  out=""\n'
        '  for a in "$@"; do case "$a" in -out=*) out="${a#-out=}";; esac; done\n'
        '  if [ -n "$out" ]; then echo fake > "$out"; fi\n'
        '  exit 0\n'
        'fi\n'
        'if [ "$1" = "show" ]; then echo \'{"resource_changes": []}\'; exit 0; fi\n'
        "exit 1\n"
    )
    shim.chmod(0o755)
    monkeypatch.setenv("TERRAFORM_BIN", str(shim))
    return types.SimpleNamespace(
        repo=repo,
        argv=lambda: [ln for ln in argv_log.read_text().splitlines() if ln.strip()],
    )


def _run_against_fake_terraform(tool, extra, fake):
    """Dispatch the tool at the fake repo and confirm the seam actually took, so
    a rejected path or a broken shim can never make one of these pass green."""
    result = _call(tool, {"tf_dir": str(fake.repo), **extra})
    argv = fake.argv()
    assert argv, (
        f"{tool} never reached the terraform shim, so this test proved nothing. "
        f"Result was: {_text(result)[:300]}"
    )
    return result, argv


@pytest.mark.parametrize("tool,extra", _TF_TOOLS)
def test_estimating_terraform_does_not_write_into_the_callers_repo(
    isolated_db, fake_terraform, tool, extra
):
    """FAILS NOW, the bug is real.

    `terraform plan -out=.plan.tmp` writes a fully resolved plan, including
    attributes Terraform marks sensitive, into whatever directory the caller
    named, and nothing ever deletes it. The default .gitignore for a Terraform
    repo covers *.tfplan, not .plan.tmp, so the next `git add -A` commits the
    customer's resolved state into their own repo.
    """
    before = {p.name for p in fake_terraform.repo.iterdir()}
    _run_against_fake_terraform(tool, extra, fake_terraform)
    after = {p.name for p in fake_terraform.repo.iterdir()}
    assert after == before, (
        f"{tool} left files behind in the caller's terraform directory: "
        f"{sorted(after - before)}"
    )


@pytest.mark.parametrize("tool,extra", _TF_TOOLS)
def test_terraform_plan_never_takes_the_customers_state_lock(
    isolated_db, fake_terraform, tool, extra
):
    """FAILS NOW, the bug is real.

    A plan against an S3+DynamoDB or TFC backend takes the remote state lock.
    subprocess.run(timeout=300) SIGKILLs terraform when the deadline fires, so
    the lock is never released and every plan and apply in that repo blocks
    until somebody runs `terraform force-unlock`. One MCP tool call from a model
    can wedge a team's deploys. `-lock=false` is the whole fix.

    Written as a conditional so it stays correct under the other valid fix,
    which is not to run plan at all.
    """
    _, argv = _run_against_fake_terraform(tool, extra, fake_terraform)
    for line in [ln for ln in argv if ln.split()[:1] == ["plan"]]:
        assert "-lock=false" in line, (
            f"{tool} ran `terraform {line}`, which takes the customer's remote "
            "state lock and never releases it on timeout"
        )


@pytest.mark.parametrize("tool,extra", _TF_TOOLS)
def test_a_tool_that_runs_terraform_plan_is_not_advertised_read_only(
    isolated_db, fake_terraform, tool, extra
):
    """FAILS NOW, the bug is real.

    tool_surface.tool_annotation defaults readOnlyHint=True for every name
    absent from WRITE_TOOLS, and server.py stamps that onto the advertised
    tool. These three are absent, so a client that auto-approves read-only
    tools spawns a process in a caller-supplied directory, takes a remote state
    lock, writes a file, and evaluates `data "external"` blocks (which execute
    arbitrary programs) without asking anyone. estimate_change_cost's own
    docstring says "Read-only: it estimates and checks, it never applies
    anything".

    Coupled to observed behaviour on purpose: if the tool stops shelling out,
    readOnlyHint=True becomes true again and this test stops objecting.
    """
    from finops.tool_surface import tool_annotation

    _, argv = _run_against_fake_terraform(tool, extra, fake_terraform)
    if not any(line.split()[:1] == ["plan"] for line in argv):
        pytest.skip(f"{tool} no longer shells out to terraform plan")
    assert tool_annotation(tool)["readOnlyHint"] is False, (
        f"{tool} spawns `terraform plan` in a caller-supplied directory but is "
        "advertised to MCP clients with readOnlyHint=True"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 4. Demo mode has no chokepoint on the MCP dispatch path
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def demo_with_tripwire_connectors(monkeypatch):
    """Demo mode on a machine that HAS real credentials, which is the documented
    enterprise-demo workflow (FINOPS_DEMO_FORCE exists for exactly this), with
    every connector replaced by one that records any attempt to fetch spend.

    The fake is at the credential boundary. A connector is the object that holds
    the customer's keys, so "was a connector asked for costs" is the question,
    not a proxy for it.
    """
    from finops import cache, demo_data

    hits: list[str] = []

    class Tripwire:
        async def is_configured(self):
            return True

        async def get_costs(self, start, end, granularity="MONTHLY"):
            hits.append("get_costs")
            raise AssertionError("live connector reached in demo mode")

        async def list_accounts(self):
            hits.append("list_accounts")
            return []

    monkeypatch.setattr(demo_data, "DEMO_MODE", True)
    monkeypatch.setattr(demo_data, "_real_provider_cache", None)
    monkeypatch.setenv("FINOPS_DEMO_FORCE", "1")
    monkeypatch.delenv("FINOPS_CONTROL_PLANE_SECRET", raising=False)
    monkeypatch.delenv("FINOPS_INSTANCE_ID", raising=False)
    # A cache hit would hide the reach rather than prevent it.
    monkeypatch.setattr(cache, "_DISABLED", True)

    for registry in (server._ALL_CONNECTORS, server.CLOUD_CONNECTORS,
                     server.SAAS_CONNECTORS):
        for key in list(registry):
            monkeypatch.setitem(registry, key, Tripwire())

    server._ACTIVE_CACHE.clear()
    assert demo_data.is_demo() is True, "fixture failed to enter demo mode"
    yield hits
    server._ACTIVE_CACHE.clear()


@pytest.mark.parametrize("tool", [
    "get_total_spend_all_sources",
    "get_costs_by_service",
    "get_top_cost_drivers",
    "get_cost_trends",
])
def test_demo_mode_never_reaches_a_live_connector(
    isolated_db, demo_with_tripwire_connectors, tool
):
    """FAILS NOW, the bug is real.

    FINOPS_DEMO_FORCE exists so a demo runs on a machine that already has real
    credentials. These tools have no is_demo() branch and no chokepoint above
    them, so on a customer screen-share the operator's own real spend is fetched
    and rendered next to StreamCo's fabricated numbers, and the real query bills
    the operator per Cost Explorer request. The Slack agent already solved this
    at one place (slack_bot.bridge.demo_bridge_result); the MCP dispatch path
    never got the equivalent.
    """
    failure = None
    try:
        _call(tool)
    except Exception as exc:   # a tripwire escaping the tool is itself evidence
        failure = exc
    assert not demo_with_tripwire_connectors, (
        f"{tool} queried {len(demo_with_tripwire_connectors)} live connector(s) "
        "while demo mode was on"
    )
    # No reach AND a crash means the tool died before the boundary, which proves
    # nothing. Fail loudly rather than banking a green.
    assert failure is None, f"{tool} failed before reaching a connector: {failure!r}"


def test_the_demo_chokepoint_control_passes_for_a_tool_that_gets_it_right(
    isolated_db, demo_with_tripwire_connectors
):
    """PASSES NOW. The control, and the guard on the one tool that is correct.

    get_cost_summary branches on is_demo() and serves the StreamCo fixture, so
    it never touches a connector. Two jobs: it proves the tripwire fixture is
    capable of going green, so the four reds above are the product and not the
    harness, and it fails if anyone deletes that branch. get_cost_summary is
    TIER1 and is the tool a demo actually opens with, so losing its demo branch
    would put the operator's real spend on the first slide.
    """
    _call("get_cost_summary")
    assert not demo_with_tripwire_connectors, (
        "get_cost_summary lost its demo branch and queried a live connector"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 5. create_api_key hands the raw credential to the model
# ══════════════════════════════════════════════════════════════════════════════

def test_create_api_key_does_not_hand_the_raw_key_to_the_model(isolated_db):
    """FAILS NOW, the bug is real.

    An MCP tool result is serialised into the conversation and transmitted to
    the model provider, which retains it in history. nbl_ keys are a plain
    SHA-256 lookup with no second factor, and the hosted box authenticates its
    whole remote MCP and REST surface with them behind Bearer-only auth. This is
    the rule connect_azure was rewritten to honour ("an Azure secret would have
    to be pasted into the chat to reach a tool argument, which routes it through
    the model provider. nable does not do that."), except here nable mints the
    credential itself, so there is no "it already existed on the machine"
    defence. The tool should return id/name/role and point at the terminal.
    """
    result = _call("create_api_key", {"name": "ci-reporter", "role": "viewer"})
    body = _text(result)
    assert "nbl_" not in body, (
        "create_api_key returned the raw credential in its MCP result, so it is "
        "shipped to the model provider and kept in conversation history"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 6. start_dashboard_server: password through the model, host bypasses expose
# ══════════════════════════════════════════════════════════════════════════════
# server_web is the private half of the open-core split and is genuinely absent
# from this repo, so the stand-in below IS the provider boundary. Every name it
# defines is one the open core reaches for by hand today.

# The xkcd example passphrase, and it is the fixture for the test asserting
# start_dashboard_server does NOT return an operator-chosen password.
_OPERATOR_PASSWORD = "correct-horse-battery-staple"  # pragma: allowlist secret


@pytest.fixture
def fake_server_web(monkeypatch):
    """Stand in for the private finops.server_web with the posture an SSO-free,
    operator-configured box actually has: a password the operator chose, so
    _PASSWORD_AUTO_GENERATED is False."""
    import sys

    import finops

    bound: dict = {}
    sw = types.ModuleType("finops.server_web")

    def start_server_background(host="127.0.0.1", port=8080):
        bound["host"] = host
        bound["port"] = port
        return object(), port

    sw.start_server_background = start_server_background
    sw._local_ip = lambda: "192.168.1.50"
    sw.set_connectors = lambda connectors: None
    sw._AUTH_DISABLED = False
    sw._DASHBOARD_PASSWORD = _OPERATOR_PASSWORD
    sw._PASSWORD_AUTO_GENERATED = False

    monkeypatch.setitem(sys.modules, "finops.server_web", sw)
    monkeypatch.setattr(finops, "server_web", sw, raising=False)
    return types.SimpleNamespace(module=sw, bound=bound)


def test_start_dashboard_server_does_not_return_an_operator_chosen_password(
    isolated_db, fake_server_web
):
    """FAILS NOW, the bug is real.

    The tool sets result["password"] unconditionally, on both the auto-generated
    and the operator-set branch. A password the operator put in
    FINOPS_DASHBOARD_PASSWORD (and may well have reused elsewhere) is serialised
    into the MCP response and shipped to the model provider. The enterprise CLI
    banner masks the same value, so the intent existed and this path missed it.
    An auto-generated per-session password is a different call; a secret the
    operator chose is not nable's to forward.
    """
    result = _call("start_dashboard_server", {"port": 8099})
    body = _text(result)
    assert "unavailable" not in body, f"the server_web stand-in did not take: {body[:300]}"
    assert _OPERATOR_PASSWORD not in body, (
        "start_dashboard_server returned the operator's own dashboard password "
        "through the model"
    )


@pytest.mark.parametrize("host", ["0.0.0.0", "::"])
def test_the_host_argument_cannot_bypass_the_expose_optin(
    isolated_db, fake_server_web, host
):
    """FAILS NOW, the bug is real.

    `bind_host = "0.0.0.0" if expose else host` carries a comment saying the
    point is that a casual "start the dashboard" never exposes a listener on the
    whole LAN. But `host` is a free-form tool argument, so a model that decides
    the user wants sharing passes host="0.0.0.0" and gets LAN-wide plain-HTTP
    exposure with expose still False, no opt-in and (for "::") no warning.
    """
    _call("start_dashboard_server", {"port": 8099, "host": host})
    assert fake_server_web.bound.get("host") in ("127.0.0.1", "::1", "localhost"), (
        f"host={host!r} with expose=False bound "
        f"{fake_server_web.bound.get('host')!r}, past the opt-in"
    )


def test_a_bind_beyond_loopback_always_carries_the_exposure_warning(
    isolated_db, fake_server_web
):
    """Whenever the listener is not on loopback, the response has to say so.

    The original defect: the warning was gated on `bind_host == "0.0.0.0"`, a
    string compare rather than a question about the address. "::" is the IPv6
    all-interfaces address and binds just as wide, so a "::" bind carried no
    exposure_warning and no share_url. The user was told the dashboard was
    running and never told it was reachable from the network in cleartext.

    This test was written to drive that through host="::", and it now SKIPS
    instead, because the sibling fix closed the door earlier in the flow: expose
    became the only gate, so "::" without expose binds 127.0.0.1 and there is
    correctly nothing to warn about. A permanent skip is a bad outcome for a
    work-list entry. It is not a pass and not a failure, so the marker would have
    sat there forever announcing unfinished work that was finished.

    Rewritten to assert the invariant its name claims rather than one route to
    it. expose=True is now the only way past loopback, so that is where the
    warning has to appear, and the check is on the BOUND ADDRESS rather than on
    any particular literal.
    """
    result = _call("start_dashboard_server", {"port": 8099, "expose": True})
    bound = fake_server_web.bound.get("host")
    body = _text(result)

    assert bound not in ("127.0.0.1", "::1", "localhost", ""), (
        f"expose=True still bound {bound!r}; this test can no longer reach a "
        f"non-loopback bind and proves nothing"
    )
    assert "exposure_warning" in body, (
        f"bound to {bound!r}, which is not loopback, with no exposure warning"
    )
    assert "cleartext" in body, (
        "the warning does not mention that the password and session cookie "
        "travel in plain HTTP, which is the part that decides whether a user "
        "should do this on their network"
    )
    assert "share_url" in body, (
        "no share_url, so a user who deliberately exposed the dashboard is not "
        "told the address other machines actually reach it on"
    )
