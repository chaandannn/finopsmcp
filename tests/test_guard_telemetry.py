"""`guard_installed` exists because the guard shipped with no telemetry at all.

We launched a feature we could not count. When the uv-cache-path bug turned up
there was no way to answer "how many people does this reach", so the answer had
to be "we cannot know". This event fixes that, and carries the one dimension
that would have answered it: whether the install repaired a dead hook.

What it must never carry: paths, commands, cost figures, anything identifying.
Counters only, and silent under NABLE_NO_TELEMETRY like every other event.
"""
from __future__ import annotations

import argparse
import io
import contextlib

import pytest

import finops.guard as g
from finops import setup_wizard


@pytest.fixture
def fired(monkeypatch, tmp_path):
    """Capture telemetry instead of sending it, and sandbox settings.json."""
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr("finops.welcome._fire_telemetry",
                        lambda e, p: events.append((e, p)))
    monkeypatch.setattr(g, "_settings_path", lambda global_scope: tmp_path / "settings.json")
    return events


def _install(**kw):
    args = argparse.Namespace(guard_action="install", guard_global=False, **kw)
    with contextlib.redirect_stdout(io.StringIO()):
        setup_wizard._run_guard(args)


def _guard_events(events):
    return [p for e, p in events if e == "guard_installed"]


def test_a_fresh_install_is_counted(fired):
    _install()
    evs = _guard_events(fired)
    assert len(evs) == 1, f"expected one guard_installed, got {evs}"
    assert evs[0]["outcome"] == "new"
    assert evs[0]["scope"] == "project"


def test_reinstalling_is_distinguishable_from_a_first_install(fired):
    _install()
    fired.clear()
    _install()
    assert _guard_events(fired)[0]["outcome"] == "already"


def test_repairing_a_dead_hook_is_counted_as_a_repair(fired, tmp_path, monkeypatch):
    """The number that tells us how far the 0.8.194 uv-cache bug actually got."""
    import json
    _install()
    p = tmp_path / "settings.json"
    s = json.loads(p.read_text())
    s["hooks"]["PreToolUse"][0]["hooks"][0]["command"] = "/gone/bin/finops guard hook"
    p.write_text(json.dumps(s))

    fired.clear()
    _install()
    assert _guard_events(fired)[0]["outcome"] == "repaired"


def test_the_hook_form_is_recorded(fired, monkeypatch):
    """Distinguishes a durable binary install from the uvx fallback, so we can
    see which form the field is actually running."""
    monkeypatch.setattr("shutil.which", lambda n: None)
    _install()
    assert _guard_events(fired)[0]["hook_form"] == "uvx"


def test_a_failed_write_is_counted_not_swallowed(fired, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(g, "install", boom)
    with pytest.raises(SystemExit):
        _install()
    evs = _guard_events(fired)
    assert evs and evs[0]["outcome"] == "write_failed"


# ── the payload must stay anonymous ───────────────────────────────────────────

def test_no_paths_commands_or_cost_data_are_sent(fired, tmp_path):
    _install()
    payload = _guard_events(fired)[0]
    blob = repr(payload)
    assert str(tmp_path) not in blob, "leaked a filesystem path"
    assert "guard hook" not in blob, "leaked the hook command"
    for key in payload:
        assert key in {"scope", "outcome", "hook_form"}, f"unexpected key {key!r}"
    for v in payload.values():
        assert isinstance(v, str) and len(v) < 32


def test_opting_out_sends_nothing(monkeypatch, tmp_path):
    """NABLE_NO_TELEMETRY is checked inside _send_event, so prove the real path
    stays silent rather than trusting the fixture's stand-in."""
    sent: list = []
    monkeypatch.setattr(g, "_settings_path", lambda global_scope: tmp_path / "settings.json")
    monkeypatch.setenv("NABLE_NO_TELEMETRY", "1")
    monkeypatch.setattr("finops.telemetry._send_event",
                        lambda *a, **k: sent.append(a))
    _install()
    assert sent == [], f"sent telemetry while opted out: {sent}"
