"""The three onboarding fixes from the 2026-08-01 funnel investigation.

The data said: 118 machines entered the interactive wizard in 30 days and 9 were
ever heard from again; 39 of 40 scan failures were an opaque "other" that failed
in 0 seconds; and dogfooding the flow on a clean machine found the Azure SDK
printing its entire failed credential chain, 1.7KB of diagnostics, into the
middle of "Step 3, see your first number".

Each fix here is pinned by the failure that motivated it, on the path a real
no-credentials machine takes, because that is the machine onboarding exists for.
"""
from __future__ import annotations

import os
import time

import pytest

from finops import ambient
from finops.cli_scan import _classify_boto_error


# ── 1. the probe is silent ────────────────────────────────────────────────────

def test_ambient_detection_prints_nothing_on_a_machine_with_no_credentials(tmp_path):
    """azure-identity logs ~25 lines at WARNING when DefaultAzureCredential finds
    nothing, which is the NORMAL case. With no logging config, Python's
    last-resort handler dumps it all to stderr in the middle of onboarding.

    This MUST run in a subprocess. Inside pytest, logging is captured by
    pytest's own handlers, so the noise never reaches stderr in-process and an
    in-process version of this test passes with the silencer deleted (proven by
    mutation). A clean interpreter with no logging config is the environment
    onboarding actually runs in, so it is the only honest place to assert."""
    import subprocess
    import sys

    r = subprocess.run(
        [sys.executable, "-c",
         "from finops import ambient; ambient.detect_all()"],
        capture_output=True, text=True, timeout=60,
        env={"HOME": str(tmp_path), "PATH": os.environ.get("PATH", ""),
             "NABLE_NO_TELEMETRY": "1"},
    )
    noise = r.stdout + r.stderr
    assert r.returncode == 0, noise[:400]
    assert noise == "", f"the probe leaked {len(noise)} bytes into onboarding:\n{noise[:400]}"


def test_quiet_loggers_restore_their_previous_state():
    import logging

    lg = logging.getLogger("azure.identity")
    before_level, before_prop = lg.level, lg.propagate
    with ambient._quiet_sdk_loggers():
        assert lg.level == logging.CRITICAL and lg.propagate is False
    assert lg.level == before_level and lg.propagate is before_prop


def test_quiet_loggers_restore_even_when_the_probe_raises():
    import logging

    lg = logging.getLogger("azure")
    before = (lg.level, lg.propagate)
    with pytest.raises(RuntimeError):
        with ambient._quiet_sdk_loggers():
            raise RuntimeError("probe blew up")
    assert (lg.level, lg.propagate) == before


# ── 2. network failures are classified, not "other" ───────────────────────────

@pytest.mark.parametrize("exc_name", [
    "EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError",
    "ProxyConnectionError", "ConnectionClosedError", "SSLError",
])
def test_connection_failures_get_their_own_class(exc_name):
    """39 of 40 real scan failures in 30 days were 'other' at duration 0s: the
    signature of a corp machine whose proxy/VPN kills the first STS call. None
    of these exception names were mapped, so the telemetry could not say so and
    the user got no fix line."""
    exc = type(exc_name, (Exception,), {})()
    assert _classify_boto_error(exc) == "network"


def test_network_class_earns_its_own_fix_line():
    # The class exists to buy a fix line; assert the render branch is wired.
    import inspect

    from finops import cli_scan

    src = inspect.getsource(cli_scan)
    assert 'klass == "network"' in src
    assert "AWS_CA_BUNDLE" in src, "the TLS-interception fix line is the whole point"


def test_scan_failure_telemetry_now_carries_version_and_exc_type(monkeypatch, capsys):
    """A month of failures was undiagnosable because events carried neither the
    version (is this a stale build?) nor the exception class (what actually
    threw?). Pin both, and pin that the MESSAGE is never sent: messages carry
    paths and account ids."""
    import sys

    from finops import cli_scan

    events = []
    monkeypatch.setattr(cli_scan, "_emit", lambda e, p, wait: events.append((e, p)))
    code = cli_scan._fail(sys.stdout, 1, ["boom"], "network", time.time(),
                          exc=ValueError("secret-path-/Users/x/.aws"))
    assert code == 1
    (event, props), = events
    assert event == "cli_scan_failed"
    assert props["error_class"] == "network"
    assert props["exc_type"] == "ValueError"
    assert props["version"], "version missing: stale-build failures stay invisible"
    assert "secret-path" not in str(props), "exception MESSAGES must never reach telemetry"


# ── 3. the cloud menu records its choice ──────────────────────────────────────

def test_the_cloud_menu_choice_is_instrumented():
    """118 machines reached this menu in 30 days; 109 were never heard from
    again, and nothing recorded what they picked. Wiring-level pin: the menu
    emits cloud_menu_choice with a mapped choice, on the no-creds path (the
    import is local because _emit_step is otherwise only bound when ambient
    credentials were found, which is exactly the wrong path to measure)."""
    import inspect

    from finops import welcome

    src = inspect.getsource(welcome)
    assert '"cloud_menu_choice"' in src
    i = src.index('"cloud_menu_choice"')
    block = src[i - 600:i + 400]
    assert "from .setup_wizard import _emit_step" in block, (
        "the emit must import locally; the outer binding only exists on the found-creds path")
    for label in ('"aws"', '"ai_keys"', '"azure"', '"gcp"', '"skip"'):
        assert label in block, f"menu choice {label} unmapped"
    assert "aborted" in block, "Ctrl-C must be distinguishable from choosing skip"


# ── 4. value comes before the editor ──────────────────────────────────────────

def test_the_first_run_shows_a_number_before_asking_about_editors():
    """The reorder itself. The old flow configured editors as step 2 and showed
    a number as step 3, so the first question a new user ever got was about MCP
    config files; 118 machines entered and 109 left without a trace. Pin the
    ORDER in the source of run_welcome_flow: the credential probe and the value
    step must both appear before the editor-config call."""
    import inspect

    from finops import welcome

    src = inspect.getsource(welcome.run_welcome_flow)
    probe = src.index("detect_all")
    value = src.index("See your first number")
    editor = src.index("_configure_mcp_clients")
    finish = src.index("You're set up.")
    assert value < probe < editor < finish, (
        "first-run order must be: value step -> credential probe -> editor config -> finish")
