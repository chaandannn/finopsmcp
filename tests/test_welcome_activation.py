"""
Activation onboarding tests.

PostHog showed the cliff: of people who install, ~84% never connect a provider
and never see a number, because the old `finops welcome` only printed a dollar
figure when the user handed over cloud credentials. If they skipped, they saw
nothing and left. These tests lock in the fix:

  1. The value moment tells the truth in demo mode (sample data, not "your account").
  2. A user who skips the credential step is never dead-ended and never shown fake
     numbers: they get an honest empty state plus the fastest real connect path.
  3. Ambient AWS credentials trigger a one-keystroke real scan, no menu.

Scans are stubbed so these stay fast and never touch a network or a real cloud.
"""
from __future__ import annotations

import subprocess

import os

import pytest

import finops.welcome as w


@pytest.fixture(autouse=True)
def _isolate_welcome_env(monkeypatch):
    """Keep these tests hermetic regardless of suite order.

    Two leak vectors: (1) the value moment sets FINOPS_DEMO on os.environ; (2)
    run_welcome_flow probes for an ambient model provider via the LLM env keys and
    the vault (_any_llm_configured / _llm_ambient_provider), so an earlier test that
    leaves an OpenAI/Anthropic credential in the vault would steer the flow down the
    LLM branch and break the AWS/demo-path assertions here. These tests cover the
    cloud/demo path only, so clear the demo + provider env keys and neutralize the
    LLM probe; tests that want the LLM branch can override.
    """
    for k in ("FINOPS_DEMO", "FINOPS_DEMO_MODE",
              "OPENAI_API_KEY", "OPENAI_ADMIN_KEY",
              "ANTHROPIC_API_KEY", "ANTHROPIC_ADMIN_KEY",
              "OPENROUTER_API_KEY"):
        monkeypatch.delenv(k, raising=False)

    async def _no_ambient_llm():
        return False

    monkeypatch.setattr(w, "_any_llm_configured", _no_ambient_llm, raising=False)
    monkeypatch.setattr(w, "_llm_ambient_provider", lambda: None, raising=False)


@pytest.fixture()
def stub_scans(monkeypatch):
    """Stub the three server scans the value moment runs, so no network is hit."""
    import finops.server as server

    async def _summary():
        return {"total_usd": 12000.0,
                "by_service": {"Amazon EC2": 7000.0, "Amazon Bedrock": 3000.0, "Amazon S3": 2000.0}}

    async def _idle():
        return {"total_monthly_waste_usd": 400.0}

    async def _ai():
        return {"addressable_savings_monthly_usd": 950.0}

    monkeypatch.setattr(server, "get_cost_summary", _summary)
    monkeypatch.setattr(server, "list_idle_resources", _idle)
    monkeypatch.setattr(server, "optimize_ai_spend", _ai)


def test_value_moment_demo_is_truthful(stub_scans, capsys):
    ok = w._show_value_moment(demo=True)
    out = capsys.readouterr().out
    assert ok is True
    assert "sample data" in out
    assert "scanned your account" not in out  # must not claim it read a real account
    assert "$12,000" in out


def test_value_moment_returns_false_on_empty(monkeypatch):
    import finops.server as server

    async def _empty():
        return {"total_usd": 0.0, "by_service": {}}

    async def _none():
        return None

    monkeypatch.setattr(server, "get_cost_summary", _empty)
    monkeypatch.setattr(server, "list_idle_resources", _none)
    monkeypatch.setattr(server, "optimize_ai_spend", _none)
    assert w._show_value_moment(demo=True) is False


def test_skip_offers_real_connect_never_fake_numbers(monkeypatch, capsys):
    """Real data or nothing: declining the credential step must not dead-end, but
    it must never invent a sample number either. It offers the fastest real path."""
    monkeypatch.setattr("finops.setup_wizard._configure_claude_desktop", lambda *a, **k: None)

    async def _no_ambient(self):
        return False

    monkeypatch.setattr("finops.connectors.aws.AWSConnector.is_configured", _no_ambient)
    monkeypatch.setattr(w, "_llm_ambient_provider", lambda: None)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "5")  # explicitly skip the menu

    calls = []
    monkeypatch.setattr(w, "_show_value_moment", lambda demo=False: calls.append(demo) or (demo is True))

    w.run_welcome_flow(demo=False)
    out = capsys.readouterr().out

    assert calls == []  # no value moment at all: no real scan, and no fake demo bill
    assert "Here's nable on a sample bill" not in out  # the old auto-demo is gone
    assert "No numbers yet, on purpose." in out  # honest empty state
    assert "finops setup aws" in out  # clear real next step offered
    assert "--demo" not in out  # never advertise sample data at a failure moment


def test_menu_default_enter_connects_not_skips(monkeypatch):
    """The no-ambient-creds menu now defaults to AWS connect on Enter, not skip, so
    the path of least resistance is to connect. Skip is a deliberate '5'."""
    monkeypatch.setattr("finops.setup_wizard._configure_claude_desktop", lambda *a, **k: None)

    async def _no_ambient(self):
        return False

    monkeypatch.setattr("finops.connectors.aws.AWSConnector.is_configured", _no_ambient)
    monkeypatch.setattr(w, "_llm_ambient_provider", lambda: None)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "")  # Enter, take the default

    connected = []
    monkeypatch.setattr("finops.setup_wizard.setup_aws_account", lambda *a, **k: connected.append(True))
    monkeypatch.setattr(w, "_show_value_moment", lambda demo=False: False)

    w.run_welcome_flow(demo=False)
    assert connected == [True]  # Enter routed into AWS connect, not the skip nudge


def test_ambient_aws_offers_one_keystroke_real_scan(monkeypatch):
    """With ambient AWS creds, accept the prompt -> real scan, no menu, no demo."""
    monkeypatch.setattr("finops.setup_wizard._configure_claude_desktop", lambda *a, **k: None)

    async def _ambient(self):
        return True

    monkeypatch.setattr("finops.connectors.aws.AWSConnector.is_configured", _ambient)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")

    calls = []
    monkeypatch.setattr(w, "_show_value_moment", lambda demo=False: calls.append(demo) or True)

    w.run_welcome_flow(demo=False)

    assert calls == [False]  # one real scan, no menu, no demo fallback


def test_slow_ambient_probe_does_not_hang_onboarding(monkeypatch):
    """A hanging credential probe (firewalled IMDS / stale SSO) must time out and
    be treated as no ambient creds, not freeze the first run."""
    monkeypatch.setattr("finops.setup_wizard._configure_claude_desktop", lambda *a, **k: None)
    monkeypatch.setattr(w, "_AMBIENT_AWS_TIMEOUT", 0.2)

    import asyncio

    async def _hang(self):
        await asyncio.sleep(5)  # far longer than the timeout
        return True

    monkeypatch.setattr("finops.connectors.aws.AWSConnector.is_configured", _hang)
    monkeypatch.setattr(w, "_llm_ambient_provider", lambda: None)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "5")  # skip the menu

    calls = []
    monkeypatch.setattr(w, "_show_value_moment", lambda demo=False: calls.append(demo) or (demo is True))

    w.run_welcome_flow(demo=False)

    # The probe timed out: ambient was treated as absent, so onboarding fell through
    # to the connect nudge rather than hanging or invoking a real scan.
    assert calls == []  # no value moment ran; the timeout did not trigger a scan


def test_demo_env_restored_after_value_moment(monkeypatch):
    """A demo scan must not leak FINOPS_DEMO into the process env afterward."""
    import os as _os

    import finops.server as server

    async def _summary():
        return {"total_usd": 100.0, "by_service": {"S3": 100.0}}

    async def _none():
        return None

    monkeypatch.setattr(server, "get_cost_summary", _summary)
    monkeypatch.setattr(server, "list_idle_resources", _none)
    monkeypatch.setattr(server, "optimize_ai_spend", _none)
    monkeypatch.delenv("FINOPS_DEMO", raising=False)
    monkeypatch.delenv("FINOPS_DEMO_MODE", raising=False)

    w._show_value_moment(demo=True)

    assert "FINOPS_DEMO" not in _os.environ
    assert "FINOPS_DEMO_MODE" not in _os.environ


def test_ambient_aws_declined_falls_through_to_menu_then_connect_nudge(monkeypatch, capsys):
    """Ambient creds present but user says no -> menu, skip, connect nudge (no fake bill)."""
    monkeypatch.setattr("finops.setup_wizard._configure_claude_desktop", lambda *a, **k: None)

    async def _ambient(self):
        return True

    monkeypatch.setattr("finops.connectors.aws.AWSConnector.is_configured", _ambient)
    monkeypatch.setattr(w, "_llm_ambient_provider", lambda: None)
    # decline Y/n, explicitly skip the menu, decline the inline sample tour
    answers = iter(["n", "5", "n"])
    monkeypatch.setattr("builtins.input", lambda *a, **k: next(answers))

    calls = []
    monkeypatch.setattr(w, "_show_value_moment", lambda demo=False: calls.append(demo) or (demo is True))

    w.run_welcome_flow(demo=False)
    out = capsys.readouterr().out

    assert calls == []  # declined real scan never ran, and no demo fallback either
    assert "No numbers yet, on purpose." in out  # honest empty state, real next step


def test_no_creds_close_never_offers_a_sample_tour(monkeypatch, capsys):
    """The close used to ask 'Want a 10-second tour on sample data?' and default it
    to YES, spending the one moment we had on fake numbers. It must never offer
    sample data here; it points at the connect flow that waits for credentials."""
    monkeypatch.setattr("finops.setup_wizard._configure_claude_desktop", lambda *a, **k: None)

    async def _no_ambient(self):
        return False

    monkeypatch.setattr("finops.connectors.aws.AWSConnector.is_configured", _no_ambient)
    monkeypatch.setattr(w, "_llm_ambient_provider", lambda: None)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "5")  # skip the menu

    calls = []
    monkeypatch.setattr(w, "_show_value_moment", lambda demo=False: calls.append(demo) or False)

    w.run_welcome_flow(demo=False)
    out = capsys.readouterr().out

    assert calls == []  # no sample tour ran, and nothing prompted for one
    assert "sample data" not in out.lower()
    assert "No numbers yet, on purpose." in out  # the honest close still printed
    assert "connect" in out  # the real path forward is what we offer instead


# ── no-ambient-creds: guide + watch instead of a dead-end paste prompt ─────────

def test_cred_fingerprint_changes_when_aws_files_appear(tmp_path, monkeypatch):
    """The watcher polls a cheap fingerprint and only pays for a real STS probe when
    something actually changed. Writing ~/.aws/credentials must move it."""
    from finops import setup_wizard as sw
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    for k in ("AWS_ACCESS_KEY_ID", "AWS_PROFILE", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(k, raising=False)

    before = sw._aws_cred_fingerprint()
    (tmp_path / ".aws").mkdir()
    (tmp_path / ".aws" / "credentials").write_text("[default]\naws_access_key_id=AKIAX\n")
    assert sw._aws_cred_fingerprint() != before

    # An env var alone also counts: that is how CI and CloudShell arrive.
    after_file = sw._aws_cred_fingerprint()
    monkeypatch.setenv("AWS_PROFILE", "acme")
    assert sw._aws_cred_fingerprint() != after_file


def test_watcher_connects_as_soon_as_credentials_appear(monkeypatch):
    """The whole point: the user configures credentials in another terminal and nable
    picks them up, instead of making them re-run the wizard (which took the one
    observed success five attempts over four hours)."""
    from finops import setup_wizard as sw
    fingerprints = iter([(1,), (1,), (2,), (2,)])
    monkeypatch.setattr(sw, "_aws_cred_fingerprint", lambda: next(fingerprints))
    monkeypatch.setattr(sw, "_detect_aws_candidates",
                        lambda: [{"account_id": "123", "label": "profile acme",
                                  "profile": "acme", "region": "us-east-1", "alias": ""}])
    monkeypatch.setattr("time.sleep", lambda s: None)

    found = sw._watch_for_aws_creds(have_ids=set(), timeout_s=30)
    assert found and found[0]["account_id"] == "123"


def test_watcher_returns_empty_on_timeout_without_hanging(monkeypatch):
    from finops import setup_wizard as sw
    monkeypatch.setattr(sw, "_aws_cred_fingerprint", lambda: (1,))
    monkeypatch.setattr(sw, "_detect_aws_candidates", lambda: [])
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert sw._watch_for_aws_creds(have_ids=set(), timeout_s=0.01) == []


def test_guide_screen_actually_renders(monkeypatch, capsys):
    """Regression: the guide printed through a name that only existed inside other
    functions, so the whole no-creds path died with NameError before it ever watched.
    Tests that stub the watcher never execute these prints, and the bug shipped. This
    one runs the real screen with only the watch loop stubbed."""
    import sys as _sys
    from finops import setup_wizard as sw

    class _Tty:
        def __init__(self, real): self._r = real
        def isatty(self): return True
        def __getattr__(self, n): return getattr(self._r, n)

    monkeypatch.setattr(sw, "_detect_sso_profiles_needing_login", lambda: [])
    monkeypatch.setattr(sw, "_watch_for_aws_creds", lambda have_ids, **kw: [])
    monkeypatch.setattr(sw, "_offer_run_aws_login", lambda *a, **k: None)  # not under test here
    monkeypatch.setattr(sw, "_emit_step", lambda *a, **k: None)
    monkeypatch.setattr(_sys, "stdin", _Tty(_sys.stdin))
    monkeypatch.setattr(_sys, "stdout", _Tty(_sys.stdout))

    assert sw._guide_and_watch_for_creds(set()) is None
    out = capsys.readouterr().out
    assert "No AWS credentials found" in out
    assert "Leave this running" in out          # the line that used to raise


def test_guided_path_offers_to_run_aws_login_inline(monkeypatch, capsys):
    """gh-style: with the AWS CLI present and no creds, the guide offers to RUN
    `aws configure sso` right here (shared stdio, so the browser flow works) instead
    of only telling the user to open a second terminal. Accepting runs it, then the
    watcher connects. This is the '5-minute, gh-login-grade' onboarding leap."""
    import sys as _sys
    from finops import setup_wizard as sw

    class _Tty:
        def __init__(self, r): self._r = r
        def isatty(self): return True
        def __getattr__(self, n): return getattr(self._r, n)

    monkeypatch.setattr(sw, "_detect_sso_profiles_needing_login", lambda: [])
    monkeypatch.setattr("shutil.which", lambda n: "/usr/bin/aws" if n == "aws" else None)
    monkeypatch.setattr(sw, "_watch_for_aws_creds", lambda have_ids, **kw: [])
    monkeypatch.setattr(sw, "_emit_step", lambda *a, **k: None)
    monkeypatch.setattr(_sys, "stdin", _Tty(_sys.stdin))
    monkeypatch.setattr(_sys, "stdout", _Tty(_sys.stdout))
    monkeypatch.setattr(sw, "_prompt", lambda *a, **k: "y")   # accept the offer
    # `aws configure sso` is v2-only. This test used to leave the version implicit,
    # which meant it passed while the code offered that command to v1 CLIs too, a
    # login that always fails. Pin v2 so this asserts the v2 behaviour it names;
    # test_onboarding_no_creds.py owns the v1 case.
    monkeypatch.setattr(sw, "_aws_cli_major", lambda: 2)

    ran = {}

    def _fake_run(cmd, *a, **k):
        ran.setdefault("cmd", cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("subprocess.run", _fake_run)

    sw._guide_and_watch_for_creds(set())
    assert ran.get("cmd") == ["aws", "configure", "sso"]      # it ran the login for us
    assert "aws configure sso" in capsys.readouterr().out


def test_guided_path_declines_run_cleanly(monkeypatch):
    """Declining the offer must NOT run anything and must fall through to watching."""
    import sys as _sys
    from finops import setup_wizard as sw

    class _Tty:
        def __init__(self, r): self._r = r
        def isatty(self): return True
        def __getattr__(self, n): return getattr(self._r, n)

    monkeypatch.setattr(sw, "_detect_sso_profiles_needing_login", lambda: [])
    monkeypatch.setattr("shutil.which", lambda n: "/usr/bin/aws" if n == "aws" else None)
    monkeypatch.setattr(sw, "_watch_for_aws_creds", lambda have_ids, **kw: [])
    monkeypatch.setattr(sw, "_emit_step", lambda *a, **k: None)
    monkeypatch.setattr(_sys, "stdin", _Tty(_sys.stdin))
    monkeypatch.setattr(_sys, "stdout", _Tty(_sys.stdout))
    monkeypatch.setattr(sw, "_prompt", lambda *a, **k: "n")   # decline
    monkeypatch.setattr(sw, "_aws_cli_major", lambda: 2)

    ran = {}

    def _fake_run(cmd, *a, **k):
        ran.setdefault("cmd", cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("subprocess.run", _fake_run)
    assert sw._guide_and_watch_for_creds(set()) is None
    assert "cmd" not in ran, "declined the offer but something still ran"
    assert "cmd" not in ran                                   # nothing was run
