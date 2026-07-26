"""The hook we write into settings.json has to still work tomorrow.

`uvx nable guard install` is the command on the landing page, and uvx runs the
package out of uv's content-addressed archive cache
(…/uv/archive-v0/<hash>/bin/finops). Persisting that absolute path produces a
guard that:

  - works when you test it,
  - stops existing on `uv cache clean`, `uv cache prune`, or the next release
    (the hash changes),
  - fails open, because Claude Code skips a hook it cannot execute, and
  - still reports itself as "installed".

That combination is worse than not installing a guard at all: it is a safety
control that lies. Reproduced against the real published 0.8.194 before this
was fixed. The rule is that an ephemeral resolution loses to the `uvx --from`
form, which re-resolves at run time.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import finops.guard as g


@pytest.fixture
def settings(tmp_path, monkeypatch):
    p = tmp_path / "settings.json"
    monkeypatch.setattr(g, "_settings_path", lambda global_scope: p)
    return p


# ── which resolutions are safe to persist ─────────────────────────────────────

def test_a_uv_archive_path_is_never_baked_into_settings(settings, monkeypatch):
    fake = "/Users/x/.cache/uv/archive-v0/_RtaPmab76BnUShh/bin/finops"
    monkeypatch.setattr("shutil.which", lambda n: fake if n == "finops" else None)
    cmd = g._hook_command()
    assert cmd == g._UVX_HOOK_CMD, (
        f"persisted a garbage-collectable uv cache path: {cmd}"
    )


@pytest.mark.parametrize("where", [
    "{home}/.cache/uv/archive-v0/abc/bin/finops",
    "{home}/Library/Caches/uv/archive-v0/abc/bin/finops",
    "{tmp}/some-scratch/bin/finops",
])
def test_ephemeral_roots_are_rejected(where, monkeypatch, tmp_path):
    import tempfile
    path = where.format(home=Path.home(), tmp=tempfile.gettempdir())
    monkeypatch.setattr("shutil.which", lambda n: path if n == "finops" else None)
    assert g._hook_command() == g._UVX_HOOK_CMD, f"{path} should be treated as ephemeral"


def test_uv_cache_dir_env_is_honoured(monkeypatch, tmp_path):
    """uv's cache is relocatable, so the check cannot hardcode ~/.cache/uv."""
    cache = tmp_path / "custom-uv-cache"
    exe = cache / "bin" / "finops"
    exe.parent.mkdir(parents=True)
    exe.touch()
    monkeypatch.setenv("UV_CACHE_DIR", str(cache))
    monkeypatch.setattr("shutil.which", lambda n: str(exe) if n == "finops" else None)
    assert g._hook_command() == g._UVX_HOOK_CMD


def test_a_real_persistent_install_is_still_preferred(monkeypatch):
    """The uvx form costs a resolve on every Bash call, so a stable binary wins."""
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    monkeypatch.setattr("shutil.which", lambda n: "/usr/local/bin/finops" if n == "finops" else None)
    assert g._hook_command() == "/usr/local/bin/finops guard hook"


def test_a_path_with_spaces_is_quoted(monkeypatch):
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    monkeypatch.setattr("shutil.which",
                        lambda n: "/Users/a b/venv/bin/finops" if n == "finops" else None)
    assert g._hook_command() == '"/Users/a b/venv/bin/finops" guard hook'


def test_nothing_on_path_falls_back_to_uvx(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda n: None)
    assert g._hook_command() == g._UVX_HOOK_CMD


# ── status must not claim a dead hook is protecting you ───────────────────────

def test_a_vanished_hook_binary_is_reported_as_broken(settings, monkeypatch, tmp_path):
    exe = tmp_path / "bin" / "finops"
    exe.parent.mkdir(parents=True)
    exe.touch(mode=0o755)
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    # pytest's tmp_path lives under $TMPDIR, which _is_ephemeral correctly
    # rejects. Point the temp root elsewhere so this exe reads as a durable
    # install, which is the case under test.
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path / "not-here"))
    monkeypatch.setattr("shutil.which", lambda n: str(exe) if n == "finops" else None)
    settings.write_text("{}")
    g.install()
    assert g.broken_hook_command(settings) is None, "healthy hook reported as broken"

    exe.unlink()                                   # uv cache clean / venv deleted
    monkeypatch.setattr("shutil.which", lambda n: None)
    broken = g.broken_hook_command(settings)
    assert broken and str(exe) in broken, (
        "a hook whose binary is gone must be reported; Claude Code fails open on it"
    )
    # is_installed stays True on purpose: the hook entry IS in the file. The
    # staleness question is what broken_hook_command answers.
    assert g.is_installed(settings) is True


def test_the_uvx_form_is_healthy_whenever_uv_exists(settings, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda n: None)
    settings.write_text("{}")
    g.install()
    assert json.loads(settings.read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"] \
        == g._UVX_HOOK_CMD
    monkeypatch.setattr("shutil.which", lambda n: "/usr/bin/uvx" if n == "uvx" else None)
    assert g.broken_hook_command(settings) is None


def test_no_hook_means_nothing_to_report(settings):
    settings.write_text("{}")
    assert g.broken_hook_command(settings) is None


def test_broken_check_never_raises_on_a_weird_file(settings):
    for body in ("not json", "[]", "null", '{"hooks": null}',
                 '{"hooks": {"PreToolUse": [{"hooks": [{"command": ""}]}]}}'):
        settings.write_text(body)
        assert g.broken_hook_command(settings) is None, f"raised or false-positived on {body!r}"


def test_the_uvx_command_carries_a_longer_timeout(settings, monkeypatch):
    """A cold uvx resolve is slow; a 10s timeout would fail open on first use."""
    monkeypatch.setattr("shutil.which", lambda n: None)
    settings.write_text("{}")
    g.install()
    hook = json.loads(settings.read_text())["hooks"]["PreToolUse"][0]["hooks"][0]
    assert hook["timeout"] >= 30, f"uvx hook got a {hook['timeout']}s timeout"
