# SPDX-License-Identifier: Apache-2.0
"""`nable connect` and `nable welcome` must not disagree about the same machine.

Measured 2026-08-19 on a home containing exactly one credential,
~/.aws/credentials, the most common one on any developer machine:

    $ nable welcome
      Found AWS credentials in your environment.

    $ nable connect
      No unconnected credentials found in the environment or config files.

Then a list of eighteen providers the person had never heard of.

The one that was wrong is the one named connect, whose own help promises to
"scan this machine for provider credentials and connect them all in one
keystroke". It scanned PROVIDER_ENV, which is eighteen API-key providers keyed
off a single environment variable each. AWS is not one of those: it is named
profiles plus a default chain, verified through STS, which is why the wizard
has a purpose-built probe for it. connect never called that probe.

This is the drifted-fork shape. The capability existed and worked; one of two
paths had it. That is also why the fix adds no new detection. Both paths now
reach `_detect_aws_candidates` through shared helpers, because a second
implementation is what caused this and a third would not help.

The tests below pin the wiring structurally rather than by substring, since a
mutation to `if False:` leaves every string in place and sails through a grep.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

import pytest


def _fn_ast(fn):
    return ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]


def _calls(node) -> set[str]:
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


def test_connect_probes_aws_at_all():
    """The whole bug in one assertion."""
    from finops import setup_scan

    called = _calls(_fn_ast(setup_scan.run_connect_command))
    assert "detect_unconnected_aws" in called, (
        "run_connect_command no longer probes for AWS, so a machine whose only "
        "credential is ~/.aws/credentials is told nothing was found by the "
        "command named connect")


def test_connect_can_actually_store_what_it_finds():
    """Detecting without connecting would be a more polite version of the bug."""
    from finops import setup_scan

    called = _calls(_fn_ast(setup_scan.run_connect_command))
    assert "connect_aws_candidate" in called, (
        "connect lists AWS accounts but never stores them, so the keystroke it "
        "advertises does nothing")


def test_both_paths_share_one_probe():
    """The anti-divergence guarantee, and the reason this bug is fixed rather
    than merely patched.

    If connect grew its own AWS detection, the two commands could drift apart
    again and the next disagreement would look exactly like the last one.
    """
    from finops import setup_wizard

    root = _calls(_fn_ast(setup_wizard.detect_unconnected_aws))
    assert "_detect_aws_candidates" in root, (
        "detect_unconnected_aws stopped delegating to the wizard's STS probe; "
        "connect is now running a second, separate implementation")

    stored = _calls(_fn_ast(setup_wizard.connect_aws_candidate))
    assert {"add_account", "_auto_aws_name"} <= stored, (
        "connect_aws_candidate no longer shares the wizard's naming and storage, "
        "so the same account connects differently depending on which command "
        "the person happened to run")


def test_a_machine_with_only_aws_is_not_told_it_has_nothing(monkeypatch, capsys):
    """The user-visible behaviour, driven end to end.

    Stubbed rather than run against real credentials: the probe calls STS, and
    a test that needs a live AWS account is a test that does not run in CI.
    monkeypatch, never bare assignment, because a leaked module attribute here
    would quietly reshape every later test in the process.
    """
    from finops import setup_scan

    candidate = {"label": "profile 'default'", "profile": "default",
                 "account_id": "123456789012", "alias": "acme", "region": "us-east-1"}

    monkeypatch.setattr(setup_scan, "scan_ambient_credentials", lambda *a, **k: [])
    monkeypatch.setattr(setup_scan, "gcloud_adc_path", lambda *a, **k: None)
    monkeypatch.setattr("finops.setup_wizard.detect_unconnected_aws",
                        lambda: [candidate])
    monkeypatch.setattr(setup_scan, "_vault_get", lambda k: None)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")   # decline, store nothing

    setup_scan.run_connect_command()
    out = capsys.readouterr().out

    assert "No unconnected credentials found" not in out, (
        "connect still reports an empty machine while holding a working AWS "
        "account in its hand")
    assert "123456789012" in out, "the account it found is not shown to the user"


def test_declining_stores_nothing(monkeypatch, capsys):
    """Consent still gates the write. Finding is not connecting."""
    from finops import setup_scan

    candidate = {"label": "profile 'default'", "profile": "default",
                 "account_id": "123456789012", "alias": "", "region": "us-east-1"}
    stored: list = []

    monkeypatch.setattr(setup_scan, "scan_ambient_credentials", lambda *a, **k: [])
    monkeypatch.setattr(setup_scan, "gcloud_adc_path", lambda *a, **k: None)
    monkeypatch.setattr("finops.setup_wizard.detect_unconnected_aws",
                        lambda: [candidate])
    monkeypatch.setattr("finops.setup_wizard.connect_aws_candidate",
                        lambda c: stored.append(c) or "acme")
    monkeypatch.setattr(setup_scan, "_vault_get", lambda k: None)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")

    setup_scan.run_connect_command()

    assert not stored, "declining the prompt still connected the account"


def test_a_probe_failure_does_not_kill_the_rest_of_the_scan(monkeypatch, capsys):
    """boto3 missing, an expired SSO token, a firewalled IMDS.

    None of those are reasons to lose the eighteen API-key providers that were
    already working before AWS was wired in.
    """
    from finops import setup_scan

    def _boom():
        raise RuntimeError("no boto3 on this machine")

    monkeypatch.setattr("finops.setup_wizard.detect_unconnected_aws", _boom)
    monkeypatch.setattr(setup_scan, "gcloud_adc_path", lambda *a, **k: None)
    monkeypatch.setattr(setup_scan, "scan_ambient_credentials", lambda *a, **k: [
        {"slug": "openai", "name": "OpenAI", "source": "environment",
         "env": {"OPENAI_API_KEY": "sk-test"}}])
    monkeypatch.setattr(setup_scan, "_vault_get", lambda k: None)
    monkeypatch.setattr(setup_scan, "connect_finding", lambda f: True)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")

    setup_scan.run_connect_command()
    out = capsys.readouterr().out

    assert "OpenAI" in out, (
        "an AWS probe failure took the whole scan down with it")
