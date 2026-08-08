"""A dependency-less install must announce itself, not fail minutes later.

Four machines on 0.8.207-0.8.209 (Linux, system Python 3.12) failed every scan
with boto3 reporting `absent`, while the published wheel declares
`boto3>=1.34.0`. So the environments installed the package without its
dependencies. The failure was correct; the timing was not. The MCP server
starts, advertises every tool, and answers tools/list looking healthy, so the
first honest signal arrived inside a scan.
"""
from __future__ import annotations

import pytest

from finops.install_health import (
    _CORE, install_health, missing_core_dependencies,
)


def test_a_complete_install_reports_healthy():
    """This test environment is a real install, so nothing may be missing."""
    assert missing_core_dependencies() == []
    assert install_health() == {"ok": True, "missing": []}


def test_a_missing_dependency_is_detected_and_named(monkeypatch):
    import finops.install_health as ih
    monkeypatch.setattr(ih, "_importable", lambda m: m != "boto3")
    assert ih.missing_core_dependencies() == ["boto3"]
    h = ih.install_health()
    assert h["ok"] is False
    assert h["missing"] == ["boto3"]
    assert "without its dependencies" in h["detail"]
    assert "force-reinstall" in h["fix"]


def test_the_verdict_says_broken_install_not_missing_feature(monkeypatch):
    """The distinction that matters: no amount of connecting providers fixes
    this, so the message must not read as a setup step."""
    import finops.install_health as ih
    monkeypatch.setattr(ih, "_importable", lambda m: m not in ("boto3", "yaml"))
    h = ih.install_health()
    assert h["reason"] == "installed without dependencies"
    assert set(h["missing"]) == {"boto3", "pyyaml"}


def test_the_check_never_raises_even_on_a_broken_meta_path(monkeypatch):
    """A diagnostic that can crash is worse than no diagnostic."""
    import finops.install_health as ih

    def explode(name):
        raise RuntimeError("broken finder")

    monkeypatch.setattr(ih.importlib.util, "find_spec", explode)
    assert ih.missing_core_dependencies() == sorted(_CORE)  # everything unverifiable
    assert ih.install_health()["ok"] is False


def test_it_uses_find_spec_rather_than_importing():
    """Importing a package runs its module-level code; a health check must not."""
    import inspect
    import finops.install_health as ih
    src = inspect.getsource(ih._importable)
    assert "find_spec" in src
    assert "__import__" not in src and "importlib.import_module" not in src


@pytest.mark.asyncio
async def test_setup_status_leads_with_a_broken_install(monkeypatch):
    """The wiring test. Without this the agent walks a user through connecting
    providers on top of an install that cannot talk to any of them."""
    import finops.install_health as ih
    monkeypatch.setattr(ih, "_importable", lambda m: m != "boto3")
    import finops.server  # noqa: F401  - normal load order; misc alone circular-imports
    from finops.tools.misc import nable_setup_status
    out = await nable_setup_status()
    assert out.get("install_broken") is True
    assert "boto3" in out["missing_packages"]
    assert "connected" not in out, "setup detail must not be offered over a broken install"
    assert "Do not" in out["next_step"]


@pytest.mark.asyncio
async def test_setup_status_is_unchanged_on_a_healthy_install():
    import finops.server  # noqa: F401
    from finops.tools.misc import nable_setup_status
    out = await nable_setup_status()
    assert "install_broken" not in out
    assert "connected" in out
