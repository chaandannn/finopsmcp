# SPDX-License-Identifier: Apache-2.0
"""Every artifact this repo publishes must state the licence this repo has.

Found 2026-08-19 while working out why a Claude directory submission bounced.
The MCPB bundle declared `Elastic-2.0`, which is not OSI-approved open source,
on a bundle that runs the Apache-2.0 package. Chasing it turned up seven
artifacts saying the same wrong thing, including `shim/pyproject.toml`, which
is why PyPI served `nable` 0.1.4 tagged `License :: Other/Proprietary License`
while `finops-mcp` alongside it was correctly Apache-2.0.

It was residue, not intent. The history is explicit:

    8a235a2  open-core relicense, Apache-2.0 for the local product and
             Elastic-2.0 for the hosted control plane   (adds LICENSE.enterprise)
    9aa5f04  open-core: remove enterprise modules,
             make repo fully Apache-2.0                 (deletes LICENSE.enterprise)

The second commit removed the file and left every reference to it behind. Two
source headers still carried `LicenseRef-Elastic-2.0` and pointed readers at
`LICENSE.enterprise` for terms, and that file has not existed since.

The consequence is not cosmetic. A licence declaration is a legal statement to
whoever installs the package, and these told people the free local tier was
source-available with a no-hosted-service restriction. Nobody would have found
it by reading code, because nothing failed.

Same shape as the stale SBOM, the frozen Releases feed and the duplicate
registry record: a published artifact asserting something untrue about the
thing it describes, silently, because nothing 404s.
"""
from __future__ import annotations

import json
import pathlib
import re
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXPECTED = "Apache-2.0"

# Everything that carries a licence string to somebody downstream.
JSON_ARTIFACTS = (
    "packaging/mcpb/manifest.json",
    "plugins/nable/.claude-plugin/plugin.json",
)
# marketplace.json carries the licence per plugin, not at the top level.
NESTED_JSON_ARTIFACTS = (
    (".claude-plugin/marketplace.json", "plugins"),
)
TOML_ARTIFACTS = (
    "pyproject.toml",
    "packaging/nable/pyproject.toml",
    "shim/pyproject.toml",
)


def _toml_license(rel: str) -> str | None:
    data = tomllib.loads((ROOT / rel).read_text(encoding="utf-8"))
    lic = data.get("project", {}).get("license")
    if isinstance(lic, dict):
        return lic.get("text")
    return lic


def test_the_repo_licence_is_the_one_we_think_it_is():
    """Anchor the expectation to the LICENSE file, not to this constant.

    Without this, someone relicensing the project would change LICENSE, watch
    every other test still pass, and ship a mismatch again.
    """
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in text and "Version 2.0" in text, (
        "LICENSE is no longer Apache-2.0. If that is deliberate, update "
        f"EXPECTED in this test and every artifact it checks; {EXPECTED} is "
        "currently asserted in 6 files")


def test_no_artifact_claims_a_different_licence():
    wrong = []
    for rel in JSON_ARTIFACTS:
        got = json.loads((ROOT / rel).read_text(encoding="utf-8")).get("license")
        if got != EXPECTED:
            wrong.append(f"{rel}: {got!r}")
    for rel, key in NESTED_JSON_ARTIFACTS:
        data = json.loads((ROOT / rel).read_text(encoding="utf-8"))
        entries = data.get(key) or []
        assert entries, f"{rel} has no {key!r} entries; this check went blind"
        for i, entry in enumerate(entries):
            got = entry.get("license")
            if got != EXPECTED:
                wrong.append(f"{rel} [{key}][{i}]: {got!r}")
    for rel in TOML_ARTIFACTS:
        got = _toml_license(rel)
        if got != EXPECTED:
            wrong.append(f"{rel}: {got!r}")

    assert not wrong, (
        "these artifacts publish a licence that is not the repo's:\n  "
        + "\n  ".join(wrong)
        + f"\n\nThe repo is {EXPECTED}. A wrong licence string here is a legal "
          "statement to whoever installs the package, and PyPI, the MCPB "
          "bundle and the plugin marketplace all read these fields directly.")


def test_no_package_carries_a_contradicting_licence_classifier():
    """The licence field is not the only thing PyPI shows, and it lost.

    Measured after shipping `nable` 0.1.5, which was cut specifically to
    correct this metadata. The `license` field read `Apache-2.0` and the page
    still said proprietary, because `classifiers` carried
    `License :: Other/Proprietary License` and PyPI renders the classifier.

    The lesson is about the previous test, not the packaging. It checked the
    field that had been wrong and stopped there, so it passed on a package that
    still published the wrong answer. Fixing the thing you were told about is
    not the same as fixing the thing.
    """
    expected = "License :: OSI Approved :: Apache Software License"
    wrong = []
    for rel in TOML_ARTIFACTS:
        data = tomllib.loads((ROOT / rel).read_text(encoding="utf-8"))
        lic = [c for c in data.get("project", {}).get("classifiers", [])
               if c.startswith("License ::")]
        if lic != [expected]:
            wrong.append(f"{rel}: {lic}")

    assert not wrong, (
        "these publish a licence classifier that contradicts the repo:\n  "
        + "\n  ".join(wrong)
        + f"\n\nPyPI renders the classifier, not just the license field, so a "
          f"wrong one here is what people actually read. Expected {expected!r}.")


def test_nothing_points_at_the_deleted_enterprise_licence():
    """`LICENSE.enterprise` was removed in 9aa5f04 and never restored.

    A file referring readers to it for terms is sending them nowhere.
    """
    assert not (ROOT / "LICENSE.enterprise").exists(), (
        "LICENSE.enterprise is back. If the open-core split is being redone, "
        "this whole test needs revisiting rather than deleting")

    # git ls-files, not rglob: it sees exactly what this repo publishes and
    # nothing it merely has on disk. An earlier rglob walked .venv and matched
    # packaging/licenses/_spdx.py, which lists every SPDX id including this one,
    # so the check failed on a third-party table rather than on our own files.
    import subprocess

    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout
    hits = []
    for rel in tracked.split("\0"):
        if not rel or rel.startswith(("CHANGELOG", "tests/test_one_license_everywhere")):
            continue
        p = ROOT / rel
        if not p.is_file() or p.suffix.lower() not in {
                ".py", ".json", ".toml", ".md", ".yml", ".yaml", ".cfg"}:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if "LICENSE.enterprise" in line or "Elastic-2.0" in line:
                hits.append(f"{rel}:{n}: {line.strip()[:100]}")

    assert not hits, (
        "these still reference the Elastic licence or the deleted "
        "LICENSE.enterprise file:\n  " + "\n  ".join(hits))
