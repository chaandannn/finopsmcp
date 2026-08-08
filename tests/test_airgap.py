"""
Tests for air-gap mode (FINOPS_AIRGAP=1).
"""
from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _restore_modules():
    """These tests delete and re-import finops.config / finops.telemetry to get
    fresh module state. Restore the originals afterward so other test files
    (and finops.server, which holds a reference) keep patching the same module
    objects regardless of execution order."""
    saved = {k: sys.modules.get(k) for k in ("finops.config", "finops.telemetry")}
    yield
    for k, v in saved.items():
        if v is not None:
            sys.modules[k] = v
        else:
            sys.modules.pop(k, None)


def _reload_config(env: dict):
    """Reload config module with a given env."""
    with patch.dict(os.environ, env, clear=False):
        if "finops.config" in sys.modules:
            del sys.modules["finops.config"]
        import finops.config as cfg
        return cfg


def test_airgap_off_by_default():
    # patch.dict(..., clear=False) cannot delete a key by omission, so blank it
    # explicitly or a developer with FINOPS_AIRGAP=1 exported sees this fail.
    cfg = _reload_config({"FINOPS_AIRGAP": ""})
    assert cfg.is_airgap() is False


def test_airgap_enabled_by_env():
    cfg = _reload_config({"FINOPS_AIRGAP": "1"})
    assert cfg.is_airgap() is True


def test_airgap_disabled_by_zero():
    cfg = _reload_config({"FINOPS_AIRGAP": "0"})
    assert cfg.is_airgap() is False


def test_airgap_disabled_by_empty():
    cfg = _reload_config({"FINOPS_AIRGAP": ""})
    assert cfg.is_airgap() is False


def test_telemetry_opted_out_in_airgap():
    """When FINOPS_AIRGAP=1, telemetry._is_opted_out() must return True."""
    with patch.dict(os.environ, {"FINOPS_AIRGAP": "1"}, clear=False):
        if "finops.telemetry" in sys.modules:
            del sys.modules["finops.telemetry"]
        import finops.telemetry as tel
        assert tel._is_opted_out() is True


def test_telemetry_not_opted_out_without_airgap():
    """With an explicit opt-in and no air-gap/NO_TELEMETRY/CI, telemetry sends.

    Telemetry became opt-in on 2026-08-08, so NABLE_TELEMETRY=1 is now part of
    the baseline here. This test isolates the AIR-GAP path, so the opt-in is set
    to keep that the only variable under test.
    """
    env_overrides = {"FINOPS_AIRGAP": "", "NABLE_NO_TELEMETRY": "",
                     "NABLE_TELEMETRY": "1"}
    # This test itself runs in CI, where telemetry is (correctly) off. Clear the
    # CI signals so we isolate the air-gap path, which is what this asserts.
    import finops.telemetry as _t
    for _v in _t._CI_ENV_VARS:
        env_overrides[_v] = ""
    with patch.dict(os.environ, env_overrides, clear=False):
        if "finops.telemetry" in sys.modules:
            del sys.modules["finops.telemetry"]
        import finops.telemetry as tel
        # Key is set in the module default; opted_out should be False
        assert tel._is_opted_out() is False


def test_airgap_check_and_warn_logs(caplog):
    """check_airgap_and_warn logs at INFO when air-gap is active."""
    import logging
    with patch.dict(os.environ, {"FINOPS_AIRGAP": "1"}, clear=False):
        if "finops.config" in sys.modules:
            del sys.modules["finops.config"]
        import finops.config as cfg
        with caplog.at_level(logging.INFO, logger="finops.config"):
            cfg.check_airgap_and_warn()
    assert any("air-gap" in r.message.lower() or "Air-gap" in r.message for r in caplog.records)


def test_airgap_no_warn_when_off(caplog):
    """check_airgap_and_warn logs nothing when air-gap is disabled."""
    import logging
    env = {k: v for k, v in os.environ.items() if k != "FINOPS_AIRGAP"}
    env["FINOPS_AIRGAP"] = ""
    with patch.dict(os.environ, env, clear=False):
        if "finops.config" in sys.modules:
            del sys.modules["finops.config"]
        import finops.config as cfg
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="finops.config"):
            cfg.check_airgap_and_warn()
    assert len(caplog.records) == 0
