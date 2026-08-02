"""Tests for the terminal-onboarding drop-off fixes.

Covers the evidence-backed friction the diagnosis flagged:
  - multi-client config writing (Cursor + Claude Code), not just Claude Desktop
  - never advertising the one-click key link while its template 404s
  - honest "which clients are wired" prose
"""
import json
import pathlib
import tempfile

from finops import setup_wizard as W
from finops import welcome as WC
from finops.security import iam_setup as I


def _tmp(name: str) -> pathlib.Path:
    return pathlib.Path(tempfile.mkdtemp()) / name


def test_merge_write_preserves_other_servers():
    p = _tmp("mcp.json")
    p.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    assert W._merge_write_mcpservers(p, {"command": "uvx", "args": ["finops-mcp"]})
    res = json.loads(p.read_text())
    assert res["mcpServers"]["other"] == {"command": "x"}
    assert res["mcpServers"]["nable"]["command"] == "uvx"


def test_merge_write_migrates_legacy_finops_key():
    p = _tmp("mcp.json")
    p.write_text(json.dumps({"mcpServers": {"finops": {"command": "old"}}}))
    W._merge_write_mcpservers(p, {"command": "new"})
    res = json.loads(p.read_text())
    assert "finops" not in res["mcpServers"]
    assert res["mcpServers"]["nable"]["command"] == "new"


def test_merge_write_refuses_unparseable_config():
    # Never clobber a config we cannot read.
    p = _tmp("mcp.json")
    p.write_text("{ not json")
    assert W._merge_write_mcpservers(p, {"command": "uvx"}) is False
    assert p.read_text() == "{ not json"


def test_build_entry_pins_version_under_uvx_when_available(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda b: "/usr/bin/uvx" if b == "uvx" else None)
    entry, display = W._build_mcp_server_entry()
    assert entry["command"] == "/usr/bin/uvx"
    assert entry["args"] and entry["args"][-1].startswith("finops-mcp")
    assert "uvx" in display


def test_uvx_args_pin_a_managed_python():
    # Every written config must force a clean managed interpreter, so an x86_64
    # conda base on Apple Silicon can't make uvx source-build for the wrong arch.
    args = W._uvx_args()
    assert args[:2] == ["--python", W._MANAGED_PYTHON]
    assert args[-1].startswith("finops-mcp")


def test_build_entry_carries_managed_python(monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda b: "/usr/bin/uvx" if b == "uvx" else None)
    entry, _ = W._build_mcp_server_entry()
    assert "--python" in entry["args"] and W._MANAGED_PYTHON in entry["args"]


def test_configure_cursor_writes_when_path_present(monkeypatch):
    target = _tmp("mcp.json")
    monkeypatch.setattr(W, "_cursor_config_path", lambda: target)
    assert W._configure_cursor({"command": "uvx", "args": ["finops-mcp"]}) is True
    assert json.loads(target.read_text())["mcpServers"]["nable"]["command"] == "uvx"


def test_configure_cursor_noop_when_cursor_absent(monkeypatch):
    monkeypatch.setattr(W, "_cursor_config_path", lambda: None)
    assert W._configure_cursor({"command": "uvx"}) is False


def test_quick_create_unavailable_for_placeholder(monkeypatch):
    monkeypatch.setattr(I, "CFN_KEY_TEMPLATE_S3_URL", I._CFN_TEMPLATE_PLACEHOLDER)
    assert I.quick_create_available() is False
    monkeypatch.setattr(I, "CFN_KEY_TEMPLATE_S3_URL", "https://real.s3.amazonaws.com/t.json")
    assert I.quick_create_available() is True


def test_one_click_is_opt_in_local_steps_are_the_default(monkeypatch, capsys):
    # Unpublished: only the fully-local console steps, no nable-hosted link.
    monkeypatch.setattr(I, "CFN_KEY_TEMPLATE_S3_URL", I._CFN_TEMPLATE_PLACEHOLDER)
    W._print_one_click_key_offer()
    out = capsys.readouterr().out
    assert "IAM -> Users" in out
    assert "console.aws.amazon.com/cloudformation" not in out

    # Published: local steps STAY the default; the one-click is shown only as an
    # optional addition, never replacing the local path.
    monkeypatch.setattr(I, "CFN_KEY_TEMPLATE_S3_URL", "https://real.s3.amazonaws.com/t.json")
    W._print_one_click_key_offer()
    out2 = capsys.readouterr().out
    assert "IAM -> Users" in out2  # local path remains the default
    assert "Optional one-click" in out2
    assert "console.aws.amazon.com/cloudformation" in out2


def test_value_moment_does_not_hang_on_blocking_scan(monkeypatch):
    # The bug: get_cost_summary can make a blocking call (SSO refresh, slow Cost
    # Explorer) that pins the event loop, so an asyncio timeout never fires and
    # setup hangs forever. The thread join must return on the wall-clock cap.
    import time
    from finops import server

    monkeypatch.setattr(WC, "_VALUE_MOMENT_TIMEOUT", 1)

    async def _block():
        time.sleep(30)  # blocking I/O on the loop, the exact hang case
        return {"grand_total_usd": 1.0}

    monkeypatch.setattr(server, "get_cost_summary", _block)
    t0 = time.monotonic()
    res = WC._value_moment_body(demo=False)
    elapsed = time.monotonic() - t0
    assert res is False
    assert elapsed < 10, f"value moment took {elapsed:.1f}s; the cap did not fire"


def test_ambient_connect_emits_provider_connected(monkeypatch):
    # The ambient-cred path (existing profile / SSO / default chain) never calls
    # setup_aws_account, so it must emit provider_connected itself, otherwise the
    # activation metric is blind to everyone who connects the easy way.
    from finops import setup_wizard
    from finops.connectors.aws import AWSConnector

    monkeypatch.setattr(WC, "_show_value_moment", lambda demo=False: True)
    monkeypatch.setattr(setup_wizard, "_configure_mcp_clients",
                        lambda: {"configured": [], "manual": []})

    # Stubs ambient.detect_all, which is what the welcome flow probes now. It
    # used to stub AWSConnector.is_configured; once the probe moved to cover every
    # cloud, that stub stopped being consulted and this test only passed on
    # machines that happen to have real AWS credentials.
    from finops.ambient import Ambient
    monkeypatch.setattr("finops.ambient.detect_all",
                        lambda providers=None: {"aws": Ambient("aws", found=True,
                                                               source="default-chain",
                                                               scopes=["123456789012"])})
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")

    emitted = []
    monkeypatch.setattr(setup_wizard, "_emit_provider_connected",
                        lambda m, provider="aws": emitted.append((m, provider)))

    WC.run_welcome_flow(demo=False)
    assert ("ambient", "aws") in emitted, (
        "the ambient connect must record WHICH provider connected; it used to "
        "hardcode aws, so an ambient Azure or GCP connect was reported as AWS"
    )


def test_demo_value_moment_renders_and_skips_real_aws_tools(monkeypatch):
    # The bug: list_idle_resources had no demo guard, so in demo mode it reached
    # for real AWS and blocked the value-moment, rendering the "sample bill" empty.
    # Demo must render the headline from get_cost_summary and never call the
    # un-guarded real-AWS tools.
    import finops.demo_data as dd
    from finops import server

    monkeypatch.setattr(dd, "DEMO_MODE", True)
    called = {"idle": 0, "ai": 0}

    async def _idle(*a, **k):
        called["idle"] += 1
        return {}

    async def _ai(*a, **k):
        called["ai"] += 1
        return {}

    monkeypatch.setattr(server, "list_idle_resources", _idle)
    monkeypatch.setattr(server, "optimize_ai_spend", _ai)

    assert WC._value_moment_body(demo=True) is True
    assert called["idle"] == 0 and called["ai"] == 0


def test_and_list_prose():
    assert WC._and_list([]) == ""
    assert WC._and_list(["Cursor"]) == "Cursor"
    assert WC._and_list(["Claude Desktop", "Cursor"]) == "Claude Desktop and Cursor"
    assert WC._and_list(["A", "B", "C"]) == "A, B, and C"


def test_no_creds_offer_leads_with_cloudshell_fast_path(capsys):
    """A no-local-key user should be pointed at AWS CloudShell first: it is
    already authenticated, so nable's ambient detection shows a real bill in
    seconds with nothing to mint. This is the lever for first value in <10 min."""
    from finops.setup_wizard import _print_one_click_key_offer

    from finops.cloudshell import COMMAND

    _print_one_click_key_offer(region="us-east-1")
    out = capsys.readouterr().out
    assert "CloudShell" in out
    # Assert the command the module OWNS, not a literal. This test previously
    # pinned "pip install finops-mcp && finops welcome", which cannot resolve on
    # CloudShell's Python 3.9 and runs the editor wizard in a shell with no
    # editor. The test encoded the bug and kept it green for months; sourcing the
    # command from cloudshell.py means the assertion can never drift from what
    # users are actually told again.
    assert COMMAND in out
    assert "pip install finops-mcp" not in out
    # The local key path must still be present, just no longer the only option.
    assert "Create access key" in out


def test_parse_combined_aws_paste_accepts_valid_pair():
    key, secret = W._parse_combined_aws_paste("AKIAABCDEFGHIJKLMNOP:supersecretkeyvalue1234567890")
    assert key == "AKIAABCDEFGHIJKLMNOP"
    assert secret == "supersecretkeyvalue1234567890"


def test_parse_combined_aws_paste_rejects_bad_input():
    assert W._parse_combined_aws_paste("") is None
    assert W._parse_combined_aws_paste("no-colon-here") is None
    assert W._parse_combined_aws_paste("short:short") is None  # both too short
    assert W._parse_combined_aws_paste("notAK1234567890123:supersecretkeyvalue1234567890") is None


def test_oneclick_aws_url_is_gated_on_publish(monkeypatch):
    """The welcome flow surfaces the one-click CFN link only when the template is
    actually published, so a no-creds user never sees a dead link, and the fast
    path lights up automatically the moment it goes live."""
    import finops.welcome as WC
    import finops.security.iam_setup as IAM

    # Unpublished (default placeholder) -> no link shown.
    monkeypatch.setattr(IAM, "quick_create_available", lambda: False)
    assert WC._oneclick_aws_url() is None

    # Published -> the real quick-create URL is surfaced.
    monkeypatch.setattr(IAM, "quick_create_available", lambda: True)
    monkeypatch.setattr(IAM, "quick_create_url",
                        lambda region="us-east-1", stack_name="nable-readonly": "https://example/launch")
    assert WC._oneclick_aws_url() == "https://example/launch"


def test_llm_ambient_provider_detects_env_keys(monkeypatch):
    """AI-native users export OPENAI_API_KEY/ANTHROPIC_API_KEY; the welcome flow
    should detect that and offer the token bill as the fast first number."""
    import finops.welcome as WC
    for k in ("OPENAI_API_KEY", "OPENAI_ADMIN_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_ADMIN_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert WC._llm_ambient_provider() is None
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert WC._llm_ambient_provider() == "OpenAI"
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", "sk-ant-admin")
    assert WC._llm_ambient_provider() == "Anthropic"


def test_llm_value_moment_renders_real_token_bill(monkeypatch, capsys):
    """The AI-native value moment must show the token bill (get_llm_costs), not a
    cloud summary that would be empty for an OpenAI-only account."""
    import finops.welcome as WC
    from finops import server

    async def _fake_llm(days=30):
        return {
            "total_usd": 14033.0,
            "by_provider": {"anthropic": 9100.0, "openai": 4933.0},
            "top_spenders": [{"model": "claude-sonnet-4-5", "cost_usd": 9100.0}],
        }

    monkeypatch.setattr(server, "get_llm_costs", _fake_llm)
    assert WC._llm_value_moment() is True
    out = capsys.readouterr().out
    assert "14,033" in out
    assert "anthropic" in out


def test_llm_value_moment_false_when_no_spend(monkeypatch):
    import finops.welcome as WC
    from finops import server

    async def _zero(days=30):
        return {"total_usd": 0.0, "by_provider": {}, "top_spenders": []}

    monkeypatch.setattr(server, "get_llm_costs", _zero)
    assert WC._llm_value_moment() is False


def test_llm_admin_key_hint_points_to_admin_keys(capsys):
    """A connected LLM key with no billing data is almost always a non-admin key.
    The flow must tell the user that with the exact next step, not dead-end on an
    empty bill, the most likely confusion on the AI-native connect path."""
    import finops.welcome as WC

    WC._llm_admin_key_hint("OpenAI")
    out = capsys.readouterr().out
    assert "admin key" in out
    assert "platform.openai.com/settings/organization/admin-keys" in out
    assert "finops setup openai" in out

    WC._llm_admin_key_hint("Anthropic")
    out = capsys.readouterr().out
    assert "admin key" in out
    assert "finops setup anthropic" in out


def test_doctor_license_check_reports_tier(monkeypatch):
    """finops doctor must surface the license tier so a user (e.g. a design partner
    activating a Team key) can confirm FINOPS_LICENSE_KEY took, see free tier with
    the activation hint, and catch an invalid/expired key."""
    import finops.doctor as D
    import finops.license as L
    from types import SimpleNamespace

    monkeypatch.setattr(L, "get_status",
                        lambda: SimpleNamespace(mode="team", email="owner@agentcard.com", days_remaining=0))
    res = D._check_license()
    assert res["ok"] is True and "Team" in res["detail"]

    monkeypatch.setattr(L, "get_status",
                        lambda: SimpleNamespace(mode="free", email="", days_remaining=0))
    res = D._check_license()
    assert res["ok"] is None and "FINOPS_LICENSE_KEY" in res["detail"]

    monkeypatch.setattr(L, "get_status",
                        lambda: SimpleNamespace(mode="invalid", email="", days_remaining=0))
    res = D._check_license()
    assert res["ok"] is False


def test_doctor_python_version_check(monkeypatch):
    """finops doctor reports the running Python: hard-fails below 3.10, and WARNS
    on 3.10 (it can only install old builds, the staleness trap), so a user on a
    stale interpreter sees the real reason rather than the cryptic pip error
    'No matching distribution found for finops-mcp'."""
    import finops.doctor as D

    monkeypatch.setattr(D.sys, "version_info", (3, 9, 18, "final", 0))
    res = D._check_python_version()
    assert res["ok"] is False
    assert "3.9" in res["detail"] and "3.11" in res["detail"]
    assert res["recommendation"]

    # 3.10 runs old builds: passes, but with the staleness warning + upgrade path.
    monkeypatch.setattr(D.sys, "version_info", (3, 10, 5, "final", 0))
    res = D._check_python_version()
    assert res["ok"] is True
    assert res["warnings"] and "OLD" in res["warnings"][0]
    assert res["recommendation"]

    monkeypatch.setattr(D.sys, "version_info", (3, 12, 1, "final", 0))
    res = D._check_python_version()
    assert res["ok"] is True
    assert "3.12" in res["detail"]
    assert res["recommendation"] is None


def test_preflight_require_python_exits_below_floor(monkeypatch):
    """The shared entry-point guard exits with a clear message on old Python and is
    a no-op on supported Python."""
    import finops._preflight as P
    import pytest

    monkeypatch.setattr(P.sys, "version_info", (3, 8, 10, "final", 0))
    with pytest.raises(SystemExit):
        P.require_python()

    monkeypatch.setattr(P.sys, "version_info", (3, 11, 5, "final", 0))
    assert P.require_python() is None


def test_normalize_and_check_key_strips_and_validates():
    """A pasted key with quotes/whitespace must be cleaned before storing, and a
    wrong-prefix paste (wrong provider, or an org id in the key field) must warn,
    so it doesn't become a silent empty bill."""
    from finops.setup_wizard import _normalize_and_check_key

    # surrounding quotes + whitespace stripped, valid prefix -> no warning
    clean, warn = _normalize_and_check_key("OPENAI_API_KEY", '  "sk-abc123"  ')
    assert clean == "sk-abc123"
    assert warn is None

    # org id pasted into the OpenAI key field -> warns
    clean, warn = _normalize_and_check_key("OPENAI_API_KEY", "org-xyz")
    assert clean == "org-xyz"
    assert warn is not None and "sk-" in warn

    # Anthropic key in the OpenAI field -> warns (wrong provider)
    _, warn = _normalize_and_check_key("OPENAI_API_KEY", "sk-ant-abc")
    assert warn is None  # 'sk-ant-' still starts with 'sk-', so OpenAI prefix passes
    _, warn = _normalize_and_check_key("ANTHROPIC_API_KEY", "sk-openai-xyz")
    assert warn is not None  # not 'sk-ant-'

    # unknown env var (e.g. an org id field) -> never warns
    clean, warn = _normalize_and_check_key("OPENAI_ORG_ID", "org-123")
    assert clean == "org-123" and warn is None


def test_value_moment_shows_llm_spend_alongside_cloud(monkeypatch, capsys):
    """A user with both cloud creds and a model provider must see the token bill
    in the same value moment, not just the (often smaller) cloud bill. The token
    bill is the AI-native hero number and the thing no cloud dashboard shows."""
    import finops.welcome as WC
    from finops import server

    async def _summary():
        return {"grand_total_usd": 1200.0, "grand_by_service": {"Amazon EC2": 1200.0}}

    async def _idle():
        return {}

    async def _ai():
        return {}

    async def _llm(days=30):
        return {"total_usd": 14033.0, "by_provider": {"anthropic": 14033.0}, "top_spenders": []}

    async def _configured():
        return True

    monkeypatch.setattr(server, "get_cost_summary", _summary)
    monkeypatch.setattr(server, "list_idle_resources", _idle)
    monkeypatch.setattr(server, "optimize_ai_spend", _ai)
    monkeypatch.setattr(server, "get_llm_costs", _llm)
    monkeypatch.setattr(WC, "_any_llm_configured", _configured)

    assert WC._value_moment_body(demo=False) is True
    out = capsys.readouterr().out
    assert "AI / LLM spend" in out
    assert "14,033" in out      # the token bill, bigger than the $1,200 cloud bill
