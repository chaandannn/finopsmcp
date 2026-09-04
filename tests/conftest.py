"""Suite-wide guards.

The OS keychain is per-macOS-user, not per-$HOME or per-FINOPS_DATA_DIR, so any
test that reaches the real `keyring` module reads (and on first-run generation,
rewrites) the developer's actual keychain. On macOS that is a password dialog
per suite run, and a rewrite resets the item ACL so "Always Allow" never sticks.
This is exactly how `test_setup_scan.py`'s Vault.default() calls turned every
pytest run into a keychain prompt.

Every test gets an in-memory keyring stub. Tests that assert keyring behavior
(test_keychain_prompts.py, test_keyring_cache.py) install their own counting
stubs on top via monkeypatch, which override this one for their duration.
"""
import os
import sys
import types
from pathlib import Path

import pytest

# Set this to run the suite against a finops installed somewhere else on
# purpose, e.g. testing a built wheel rather than the working tree.
_ALLOW_FOREIGN = "FINOPS_TESTS_ALLOW_FOREIGN_SOURCE"


def pytest_configure(config):
    """Refuse to run this tree's tests against a different tree's source.

    finops is installed editable, pointing at ONE checkout. A git worktree gets
    its own tests/ and its own src/, but `import finops` still resolves to
    whichever checkout the install points at, so running pytest from a worktree
    silently pairs that worktree's test files with the main checkout's code.

    Nothing errors. You get a normal-looking run whose result means nothing, and
    the failures it invents look exactly like real ones. On 2026-08-22 that cost
    two wrong conclusions in a row about the same test: first "this fails on
    main" (it did not; the source was a feature branch), then "this branch broke
    it" (it had not; the test had been renamed on that branch, so the old name
    was being run against new code). Both readings were confidently reported
    before anyone noticed the trees were crossed.

    The fix a person needs is one environment variable, so the message says it
    rather than describing the problem and leaving them to it. Failing here in
    pytest_configure stops the run with a single line, instead of thousands of
    confusing test failures further down.
    """
    if os.environ.get(_ALLOW_FOREIGN, "").strip().lower() in ("1", "true", "yes"):
        return
    try:
        import finops
    except Exception:                      # pragma: no cover - collection handles it
        return

    tests_tree = Path(__file__).resolve().parent.parent
    expected = (tests_tree / "src" / "finops").resolve()
    actual = Path(finops.__file__).resolve().parent
    if actual == expected:
        return

    pytest.exit(
        "\n"
        "  These tests and the finops they import are from different trees.\n\n"
        f"    tests come from : {tests_tree}\n"
        f"    finops imports  : {actual}\n"
        f"    expected        : {expected}\n\n"
        "  finops is installed editable against one checkout, so a worktree's\n"
        "  tests run against the main checkout's source unless you say otherwise.\n"
        "  Whatever this run reported would have been about neither tree.\n\n"
        f"    PYTHONPATH={tests_tree / 'src'} pytest ...\n\n"
        f"  Deliberately testing an installed build? Set {_ALLOW_FOREIGN}=1.\n",
        returncode=4,
    )


@pytest.fixture(autouse=True)
def _no_real_keychain(monkeypatch):
    store: dict = {}
    fake = types.ModuleType("keyring")
    fake.get_password = lambda service, user: store.get((service, user))
    fake.set_password = lambda service, user, value: store.__setitem__((service, user), value)
    fake.delete_password = lambda service, user: store.pop((service, user), None)
    monkeypatch.setitem(sys.modules, "keyring", fake)

    # The vault and trial caches are module-global; clear them so a key cached
    # by one test never satisfies (or poisons) another.
    from finops.security.vault import Vault
    import finops.license as license_mod
    Vault._key_cache.clear()
    monkeypatch.setattr(license_mod, "_kr_cached_date", None)
    yield
    Vault._key_cache.clear()


@pytest.fixture(autouse=True)
def _no_ambient_service_creds(monkeypatch):
    """Unit tests must not inherit the developer's live service credentials.

    A dev box with GITHUB_TOKEN + GITHUB_ORGS set made an unpatched code path do a
    real GitHub org sweep inside a unit test (4-5s per run, network-flaky). Strip
    the vars suite-wide; tests that need them set their own via monkeypatch, which
    layers on top of this fixture for their duration.
    """
    for var in ("GITHUB_TOKEN", "GITHUB_ORGS"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True, scope="session")
def _no_test_may_spend_money():
    """Hard block on every billed AWS API call for the whole test session.

    This exists because a test called `test_explain_cost_drivers_demo_answers`
    was making two real ce:GetCostAndUsage requests against the developer's own
    AWS account on every suite run. It patched demo_data.DEMO_MODE but not
    FINOPS_DEMO_FORCE, and is_demo() yields to real data whenever a provider is
    connected, so on any machine with credentials the "demo" test quietly
    exercised the live path and billed the owner per request.

    Nothing in a test suite should be able to do that, and no reviewer should
    have to notice it. Cost Explorer bills per request; Athena bills per byte
    scanned. Both raise here with the name of the test that reached them.

    A test that genuinely needs to exercise these must stub the client itself.
    """
    import botocore.client

    billed = {
        "ce": "Cost Explorer (billed per request)",
        "athena": "Athena (billed per byte scanned)",
        "cost-optimization-hub": "Cost Optimization Hub (billed per request)",
    }
    original = botocore.client.BaseClient._make_api_call

    def guarded(self, operation_name, api_params):
        service = self.meta.service_model.service_name
        if service in billed:
            raise AssertionError(
                f"A test reached {service}.{operation_name} — {billed[service]}. "
                f"This spends real money on whoever runs the suite. Stub the "
                f"client, or use demo data with FINOPS_DEMO_FORCE=1."
            )
        return original(self, operation_name, api_params)

    botocore.client.BaseClient._make_api_call = guarded
    try:
        yield
    finally:
        botocore.client.BaseClient._make_api_call = original
