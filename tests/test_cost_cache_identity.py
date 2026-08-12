# SPDX-License-Identifier: Apache-2.0
"""One account's spend must never be served as another's.

Why this file exists, stated plainly: the cost cache key identified the provider
and the date range and nothing else. `make_key("connector.get_costs", name,
start, end, granularity)` has no account in it, and the AWS-internal key used
AWS_ROLE_ARNS, which is one environment variable every connector in the process
shares, collapsing to the literal "default" whenever a session was injected
instead.

get_cost_summary_all_accounts builds one AWSConnector per account inside a single
tool call. Account #1 populated the entry and accounts #2..N read it back, so
every account in the org reported account #1's spend. Because the TTL is 12 hours
and that exceeds the persistence threshold, the wrong answer was also pickled to
disk and served again after a restart. FINOPS_PROFILE compounded it: storage/db.py
gives each profile its own database and vault, but the disk cache was keyed only
on FINOPS_DATA_DIR, so two profiles pointed at two different customers shared one
cache file.

The principle these tests pin: when a connector cannot say whose numbers it
holds, it must not share cache entries. A miss costs one API call. A wrong hit
costs a customer a wrong bill, silently, in a product whose entire claim is that
the numbers are trustworthy.

Nothing here touches AWS. The connectors are never asked to fetch; only the key
they would look under is examined, which is the thing that was wrong.
"""
from __future__ import annotations

import importlib
import os
import pathlib

import pytest

from finops.connectors.aws import AWSConnector


class _FakeSession:
    """Stands in for a boto3.Session built per account by get_boto3_session."""

    def __init__(self, profile_name: str | None = None):
        self.profile_name = profile_name


def test_two_accounts_do_not_share_a_cache_identity():
    """The bug, at its narrowest.

    Both connectors carry an injected session and identical AWS_ROLE_ARNS. Before
    the fix both produced "default" and collided.
    """
    a = AWSConnector(session=_FakeSession(), identity="111122223333")
    b = AWSConnector(session=_FakeSession(), identity="444455556666")
    assert a.cache_identity() != b.cache_identity()


def test_the_same_account_still_shares_its_own_entry():
    """The cache must still cache. Fixing this by disabling it is not a fix."""
    a = AWSConnector(session=_FakeSession(), identity="111122223333")
    b = AWSConnector(session=_FakeSession(), identity="111122223333")
    assert a.cache_identity() == b.cache_identity()


def test_an_unidentified_session_opts_out_of_sharing():
    """A connector that cannot prove whose account it reads must not share.

    This is the safety default. Two connectors holding anonymous sessions might
    be the same account or might be two customers, and nothing at this layer can
    tell. Not sharing costs an API call; sharing costs a wrong bill.
    """
    a = AWSConnector(session=_FakeSession())
    b = AWSConnector(session=_FakeSession())
    assert a.cache_identity() != b.cache_identity()


def test_a_named_profile_is_a_usable_identity():
    """Distinct AWS CLI profiles are distinct credentials, so they may key."""
    a = AWSConnector(session=_FakeSession(profile_name="prod"))
    b = AWSConnector(session=_FakeSession(profile_name="staging"))
    assert a.cache_identity() != b.cache_identity()
    assert a.cache_identity() == AWSConnector(
        session=_FakeSession(profile_name="prod")).cache_identity()


def test_no_session_falls_back_to_the_env_role_set(monkeypatch):
    """The single-account path keeps its old, correct behaviour."""
    monkeypatch.delenv("AWS_ROLE_ARNS", raising=False)
    assert AWSConnector().cache_identity() == "env:default"
    monkeypatch.setenv("AWS_ROLE_ARNS", "arn:aws:iam::111122223333:role/nable")
    assert "111122223333" in AWSConnector().cache_identity()


def test_every_per_account_connector_passes_an_identity():
    """The call site, not just the helper, and by AST rather than by string.

    Testing cache_identity alone would leave any loop free to keep constructing
    AWSConnector(session=...) with no identity, which is the exact shape of the
    original bug. An earlier version of this test matched a source string, which
    is whitespace sensitive: a reformat would have turned it red for a reason
    that has nothing to do with cache correctness, and a differently indented
    call site would have slipped past it green. Both are failures for the wrong
    reason.

    So this parses the tree and checks the property directly: anywhere in the
    package, a connector built with an injected session must also be told whose
    account it is. That holds under any formatting, and it covers call sites
    nobody has written yet.
    """
    import ast

    import finops

    root = pathlib.Path(finops.__file__).parent
    offenders = []
    for path in root.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name != "AWSConnector":
                continue
            kwargs = {k.arg for k in node.keywords if k.arg}
            if "session" in kwargs and "identity" not in kwargs:
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")

    assert not offenders, (
        "These build an AWSConnector with an injected session but never say which "
        "account it reads, so every one of them shares a cache entry keyed on an "
        "env var they all have in common:\n  " + "\n  ".join(offenders)
    )


def test_two_profiles_do_not_share_one_cache_file(monkeypatch, tmp_path):
    """FINOPS_PROFILE separates the DB and the vault; it must separate this too."""
    import finops.cache as cache

    monkeypatch.setenv("FINOPS_DATA_DIR", str(tmp_path))

    monkeypatch.setenv("FINOPS_PROFILE", "customer-a")
    importlib.reload(cache)
    conn_a = cache._disk_conn()
    monkeypatch.setenv("FINOPS_PROFILE", "customer-b")
    conn_b = cache._disk_conn()

    files = sorted(f for f in os.listdir(tmp_path) if f.endswith(".db"))
    for c in (conn_a, conn_b):
        if c is not None:
            c.close()
    assert len(files) >= 2, (
        f"both profiles wrote to {files}: two customers sharing one cache file"
    )
