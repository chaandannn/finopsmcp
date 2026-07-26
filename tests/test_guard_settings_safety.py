"""`nable guard install` writes to a file the user did not give us: their
Claude Code settings.json. That file may already hold hooks from other tools,
their model choice, their permissions. The bar is not "usually works".

Every test here is a state a real machine can be in. The invariant in all of
them is the same: either we make the intended change, or we change *nothing*
and say why. Never a partial write, never a repair we guessed at, never a
traceback that reads as "nable is broken" when the fix is one chmod.

Found by stress-testing the install path before the Product Hunt launch: a
top-level JSON array and a `"hooks": null` both parsed fine and then raised a
raw AttributeError from inside setdefault.
"""
from __future__ import annotations

import json
import stat

import pytest

import finops.guard as g


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """Point the guard at a throwaway settings.json and hand back the path."""
    p = tmp_path / "settings.json"
    monkeypatch.setattr(g, "_settings_path", lambda global_scope: p)
    return p


def _commands(path):
    s = json.loads(path.read_text())
    return [h.get("command", "") for e in (s.get("hooks") or {}).get("PreToolUse") or []
            for h in (e.get("hooks") or [])]


# ── shapes that parse as JSON but are not what we expect ──────────────────────

@pytest.mark.parametrize("body,described_as", [
    ('["not", "a", "dict"]', "list"),
    ('"a bare string"', "str"),
    ("42", "int"),
])
def test_non_object_settings_refused_without_touching_the_file(settings, body, described_as):
    settings.write_text(body)
    with pytest.raises(SystemExit) as e:
        g.install()
    assert settings.read_text() == body, "refused but still wrote to the file"
    assert described_as in str(e.value) and "nothing was changed" in str(e.value)


def test_malformed_json_refused_without_touching_the_file(settings):
    body = '{"model": "opus", THIS IS NOT JSON'
    settings.write_text(body)
    with pytest.raises(SystemExit) as e:
        g.install()
    assert settings.read_text() == body
    assert "not valid JSON" in str(e.value) and "nothing was changed" in str(e.value)


@pytest.mark.parametrize("body", [
    '{"hooks": null}',                       # a real state: some tools write this
    '{"hooks": {"PreToolUse": null}}',
])
def test_null_hook_keys_are_treated_as_absent_not_as_a_crash(settings, body):
    settings.write_text(body)
    g.install()
    assert any("guard hook" in c for c in _commands(settings))


@pytest.mark.parametrize("body,described_as", [
    ('{"hooks": "wat"}', "str"),
    ('{"hooks": {"PreToolUse": {"not": "a list"}}}', "dict"),
])
def test_wrong_typed_hook_keys_refused_rather_than_guessed_at(settings, body, described_as):
    settings.write_text(body)
    with pytest.raises(SystemExit) as e:
        g.install()
    assert settings.read_text() == body
    assert described_as in str(e.value)


def test_is_installed_never_raises_on_a_file_it_cannot_understand(settings):
    """It is a read-only predicate. False is always a safe answer; a traceback
    is not, because callers use it to decide whether to even try."""
    for body in ('[]', 'null', '"x"', '{"hooks": null}', 'not json at all',
                 '{"hooks": {"PreToolUse": [null]}}',
                 '{"hooks": {"PreToolUse": [{"hooks": [null]}]}}'):
        settings.write_text(body)
        assert g.is_installed(settings) is False, f"raised or true-d on {body!r}"


# ── other people's config is not ours to break ────────────────────────────────

def test_a_foreign_pretooluse_hook_survives_install_and_uninstall(settings):
    settings.write_text(json.dumps({"model": "opus", "hooks": {"PreToolUse": [
        {"matcher": "Bash",
         "hooks": [{"type": "command", "command": "some-other-tool check"}]}]}}))
    g.install()
    assert any("some-other-tool" in c for c in _commands(settings))
    assert any("guard hook" in c for c in _commands(settings))

    g.uninstall()
    left = _commands(settings)
    assert any("some-other-tool" in c for c in left), "we removed a hook that was not ours"
    assert not any("guard hook" in c for c in left), "our own hook was left behind"
    assert json.loads(settings.read_text())["model"] == "opus"


def test_uninstall_leaves_unrecognized_entries_exactly_as_found(settings):
    """A malformed entry inside an otherwise fine file is still someone's data."""
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [
        "a bare string entry",
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "keep-me"}]},
    ]}}))
    g.install()
    g.uninstall()
    entries = json.loads(settings.read_text())["hooks"]["PreToolUse"]
    assert "a bare string entry" in entries


def test_install_is_idempotent(settings):
    settings.write_text("{}")
    for _ in range(3):
        g.install()
    assert len([c for c in _commands(settings) if "guard hook" in c]) == 1


def test_uninstall_twice_is_not_an_error(settings):
    settings.write_text("{}")
    g.install()
    assert g.uninstall() is True
    assert g.uninstall() is False


def test_missing_parent_directories_are_created(tmp_path, monkeypatch):
    p = tmp_path / "nested" / "deeper" / "settings.json"
    monkeypatch.setattr(g, "_settings_path", lambda global_scope: p)
    g.install()
    assert p.exists()


def test_an_unwritable_settings_file_does_not_corrupt_it(settings):
    settings.write_text(json.dumps({"model": "opus"}))
    settings.chmod(stat.S_IRUSR)
    try:
        with pytest.raises(OSError):
            g.install()
    finally:
        settings.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert json.loads(settings.read_text()) == {"model": "opus"}
