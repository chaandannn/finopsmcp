"""
Version-pin + finops upgrade tests.

The footgun: configs that launch `uvx finops-mcp` unpinned re-resolve "latest"
on the first cold start after every PyPI release, which can blow past Claude
Desktop's startup timeout ("Server disconnected"). The fix: the wizard writes
a pinned `finops-mcp==X`, and `finops upgrade` moves the pin deliberately,
warming the cache outside any client startup window.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import finops.setup_wizard as sw


def test_pinned_package_uses_installed_version():
    pinned = sw._pinned_package()
    assert pinned.startswith("finops-mcp==")
    assert pinned == f"finops-mcp=={sw._installed_version()}"


def test_pinned_package_explicit_target():
    assert sw._pinned_package("9.9.9") == "finops-mcp==9.9.9"


def test_pinned_package_falls_back_unpinned(monkeypatch):
    monkeypatch.setattr(sw, "_installed_version", lambda: "")
    assert sw._pinned_package() == "finops-mcp"


def _fake_config(tmp_path, args):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({
        "mcpServers": {"nable": {"command": "/usr/bin/uvx", "args": args}}
    }))
    return cfg


def _run_upgrade_with(monkeypatch, cfg, target, warm_rc=0):
    monkeypatch.setattr(sw, "_claude_config_file", lambda: cfg)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/uvx" if name == "uvx" else None)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=warm_rc, stdout="", stderr="boom" if warm_rc else "")

    monkeypatch.setattr("subprocess.run", fake_run)
    sw._run_upgrade(target)
    return calls


def test_upgrade_moves_the_pin(tmp_path, monkeypatch, capsys):
    cfg = _fake_config(tmp_path, ["finops-mcp==0.8.50"])
    calls = _run_upgrade_with(monkeypatch, cfg, "9.9.9")

    # Cache warm ran against the exact target, outside any client startup
    assert any("finops-mcp==9.9.9" in " ".join(c) for c in calls)
    saved = json.loads(cfg.read_text())
    assert saved["mcpServers"]["nable"]["args"] == ["finops-mcp==9.9.9"]
    assert "Restart Claude Desktop" in capsys.readouterr().out


def test_upgrade_pins_legacy_unpinned_entry(tmp_path, monkeypatch):
    cfg = _fake_config(tmp_path, ["finops-mcp"])
    _run_upgrade_with(monkeypatch, cfg, "9.9.9")
    saved = json.loads(cfg.read_text())
    assert saved["mcpServers"]["nable"]["args"] == ["finops-mcp==9.9.9"]


def test_failed_cache_warm_leaves_config_untouched(tmp_path, monkeypatch, capsys):
    """If the new version can't even be downloaded, never break the working pin."""
    cfg = _fake_config(tmp_path, ["finops-mcp==0.8.50"])
    _run_upgrade_with(monkeypatch, cfg, "9.9.9", warm_rc=1)
    saved = json.loads(cfg.read_text())
    assert saved["mcpServers"]["nable"]["args"] == ["finops-mcp==0.8.50"]
    assert "NOT changed" in capsys.readouterr().out


def test_upgrade_actually_upgrades_the_running_cli(monkeypatch, capsys):
    """The bug: `finops upgrade` warmed the uvx cache but left the pip-installed
    `finops` command on the old version, so `finops <newcmd>` still failed right
    after a 'success' message. It must run pip install -U on the current env."""
    monkeypatch.setattr(sw, "_installed_version", lambda: "0.8.158")
    ran = []

    def fake_run(cmd, **kw):
        ran.append(cmd)
        # `pip show finops-mcp` -> a normal (non-editable) install
        if "show" in cmd:
            return SimpleNamespace(returncode=0, stdout="Name: finops-mcp\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    assert sw._upgrade_running_cli("0.8.158", "0.8.185") is True
    installs = [c for c in ran if "install" in c and "-U" in c]
    assert installs and "finops-mcp==0.8.185" in " ".join(installs[0])
    assert "upgraded to 0.8.185" in capsys.readouterr().out


def test_upgrade_never_clobbers_an_editable_checkout(monkeypatch, capsys):
    def fake_run(cmd, **kw):
        assert "install" not in cmd, "must not pip-install over an editable dev tree"
        return SimpleNamespace(returncode=0, stdout="Editable project location: /src\n", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    assert sw._upgrade_running_cli("0.8.185", "0.9.0") is False
    assert "editable" in capsys.readouterr().out.lower()


def test_upgrade_non_pip_install_prints_manual_command(monkeypatch, capsys):
    def fake_run(cmd, **kw):
        return SimpleNamespace(returncode=1, stdout="", stderr="not found")  # pip show fails

    monkeypatch.setattr("subprocess.run", fake_run)
    assert sw._upgrade_running_cli("0.8.158", "0.8.185") is False
    out = capsys.readouterr().out
    assert "pip install -U 'finops-mcp==0.8.185'" in out


def test_plugin_pin_matches_package_version():
    """The Claude Code plugin pins the server version. If a release bumps
    pyproject but forgets the plugin pin, installs would silently run an old
    server. This fails the suite at release time instead."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    pyproject = (root / "pyproject.toml").read_text()
    pkg_version = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)

    plugin = json.loads((root / "plugins/nable/.claude-plugin/plugin.json").read_text())
    args = plugin["mcpServers"]["nable"]["args"]
    # The finops-mcp token is pinned and last; a managed --python prefix is fine.
    assert args[-1] == f"finops-mcp=={pkg_version}"
    assert "--python" in args
    assert plugin["version"] == pkg_version


def test_the_published_sbom_describes_the_package_being_published():
    """docs/sbom.json is a public claim about our dependencies, and it lied.

    Found 2026-08-17: it was frozen at 0.8.36, generated on May 31, declaring
    mcp 1.3.0 while the tree required a version 25 releases newer. It is served
    from docs/ and is the artifact an enterprise security reviewer asks for, so a
    stale one is worse than none: it is a specific, checkable, false statement
    about what we ship, and the reviewer who checks it is exactly the reader we
    are trying to convince.

    scripts/generate_sbom.py exists and nobody ran it, which is the whole story.
    A generated artifact with no test is a snapshot of the day someone remembered.
    """
    import json
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    pkg_version = re.search(r'^version = "([^"]+)"',
                            (root / "pyproject.toml").read_text(), re.M).group(1)

    sbom = json.loads((root / "docs/sbom.json").read_text())
    declared = sbom["metadata"]["component"]["version"]

    assert declared == pkg_version, (
        f"docs/sbom.json describes {declared} and we are shipping {pkg_version}. "
        f"Regenerate it: python scripts/generate_sbom.py")


def test_the_sbom_agrees_with_the_dependency_floors_it_claims_to_describe():
    """Version drift is not the only way an SBOM goes false.

    A component whose declared version is below the floor pyproject requires is a
    claim we are shipping something we cannot be shipping, which is how mcp 1.3.0
    survived a floor of 1.28. This compares every component the SBOM lists against
    the requirement it was generated from.
    """
    import json
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    pyproject = (root / "pyproject.toml").read_text()
    sbom = json.loads((root / "docs/sbom.json").read_text())

    # Reuse the generator's own parser rather than a second regex. A test that
    # re-implements its subject cannot fail against it, and this comparison is
    # only meaningful if both sides read the requirement the same way. My first
    # version used its own pattern, missed every dependency, compared nothing,
    # and passed: the mutation check caught it, which is what mutation checks are
    # for.
    import sys
    sys.path.insert(0, str(root / "scripts"))
    import generate_sbom

    meta = generate_sbom._parse_toml_simple((root / "pyproject.toml").read_text())
    floors: dict[str, str] = {}
    for raw in (meta.get("dependencies") or []):
        name, version = generate_sbom._parse_dep(raw)
        if version:
            floors[name.lower().replace("_", "-")] = version

    assert floors, "parsed no dependency floors, so this test compares nothing"

    def _tup(v: str) -> tuple:
        return tuple(int(x) for x in re.findall(r"\d+", v)[:3])

    stale = []
    for c in sbom.get("components", []):
        name = c["name"].lower().replace("_", "-")
        if name in floors and _tup(c["version"]) < _tup(floors[name]):
            stale.append(f"{c['name']} {c['version']} < required {floors[name]}")

    assert not stale, (
        "the SBOM declares versions we do not ship:\n  " + "\n  ".join(stale))


def test_dunder_version_matches_package():
    """The one the CLI actually reads, and the one nobody guarded.

    _installed_version() prefers finops.__version__ over dist metadata on
    purpose: metadata freezes at install time and went stale on editable trees
    (it reported 0.8.36 on a 0.8.171 tree). That makes __version__ the source of
    truth for `nable --version`, for the telemetry every run reports itself
    under, and, worst, for the pin written into the user's editor config.

    Measured on a clean install of 0.8.211 from PyPI: pyproject said 0.8.211,
    __init__.py still said 0.8.210, and setup offered to write
    `uvx --python 3.12 finops-mcp==0.8.210` into Claude Desktop. The release was
    published and could not reach anyone who onboarded after it.

    This drift already had two tests, on plugin.json and server.json, both added
    after a release failed silently the same way. The file the CLI reads had
    none. That is what this is.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    pyproject = (root / "pyproject.toml").read_text()
    pkg_version = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)

    init = (root / "src/finops/__init__.py").read_text()
    dunder = re.search(r'^__version__ = "([^"]+)"', init, re.M).group(1)

    assert dunder == pkg_version, (
        f"src/finops/__init__.py says {dunder} and pyproject says {pkg_version}. "
        f"The CLI reports the former, so every editor config written by setup "
        f"would pin finops-mcp=={dunder} and this release would reach nobody who "
        f"onboards."
    )


def test_the_version_the_cli_reports_is_the_version_that_gets_pinned():
    """Ties the two halves together, so moving one without the other fails.

    The bug was not that a constant was stale. It was that the stale constant
    silently became the version string in somebody else's Claude Desktop config.
    """
    import pathlib
    import re

    from finops.setup_wizard import _installed_version

    root = pathlib.Path(__file__).resolve().parents[1]
    pyproject = (root / "pyproject.toml").read_text()
    pkg_version = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)

    assert _installed_version() == pkg_version, (
        "the version the CLI reports, and writes into editor configs, is not the "
        "version being released"
    )


def test_server_json_version_matches_package():
    """server.json feeds the MCP Registry publish, which rejects a duplicate or
    stale version. If a release bumps pyproject but forgets server.json, the
    registry leg fails (0.8.78 and 0.8.79 both did, silently, with no test here).
    This fails the suite at release time instead."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    pyproject = (root / "pyproject.toml").read_text()
    pkg_version = re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)

    server = json.loads((root / "server.json").read_text())
    # The registry version and the PyPI package it points at must both track the
    # package version, or registry-discovered installs pin to a stale release.
    assert server["version"] == pkg_version
    assert server["packages"][0]["version"] == pkg_version


def test_server_json_description_within_registry_limit():
    """The MCP Registry hard-caps server.json `description` at 100 chars and
    rejects the whole publish with a 422 if exceeded. A 243-char description
    shipped in 0.8.125 silently broke the registry leg for three releases (the
    registry froze at 0.8.124 while PyPI moved on) because nothing checked the
    length. This fails the suite at edit time instead."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    server = json.loads((root / "server.json").read_text())
    desc = server["description"]
    assert len(desc) <= 100, (
        f"server.json description is {len(desc)} chars; the MCP Registry rejects "
        f"anything over 100 with a 422 and the publish silently fails: {desc!r}"
    )


def test_upgrade_preserves_other_args(tmp_path, monkeypatch):
    """Only the finops-mcp token moves; any other args stay put."""
    cfg = _fake_config(tmp_path, ["--python", "3.12", "finops-mcp==0.8.50"])
    _run_upgrade_with(monkeypatch, cfg, "9.9.9")
    saved = json.loads(cfg.read_text())
    assert saved["mcpServers"]["nable"]["args"] == ["--python", "3.12", "finops-mcp==9.9.9"]
