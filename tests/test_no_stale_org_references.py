# SPDX-License-Identifier: Apache-2.0
"""Nothing we publish may point at the namespace we walked away from.

The account was renamed from `chaandannn` to `getnable`. GitHub 301-redirects
the old paths, which is exactly what makes this hard to see: nothing 404s, no
badge visibly breaks, no link dead-ends. The staleness is invisible until you
go looking for it.

It is not cosmetic. `io.github.chaandannn/*` is an orphaned MCP registry
namespace: the GitHub user no longer exists, so nobody can authenticate to it,
and the username is released and claimable by a stranger. Every artifact of
ours still naming that path is an artifact pointing somewhere we do not
control and cannot reclaim.

This is the fourth time this shape has bitten: the SBOM described a release
eleven weeks old, the uvx shim served a five-week-old build, an awesome-list
entry carried a Glama badge that 404'd, and the MCP registry carried a
duplicate 36 releases behind. Each was found by hand, one at a time. A ratchet
is cheaper than remembering.
"""
from __future__ import annotations

import pathlib
import subprocess
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]

# The GitHub account this project used to live under. Renamed, released, and
# claimable by anyone; see the module docstring.
RETIRED_OWNER = "chaandannn"
CURRENT_REPO = "https://github.com/getnable/finopsmcp"


def _tracked_text_files() -> list[pathlib.Path]:
    """Every file git actually publishes, skipping binaries."""
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    files = []
    for rel in out.split("\0"):
        if not rel:
            continue
        p = ROOT / rel
        if not p.is_file() or p.suffix.lower() in {
                ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".mp4",
                ".woff", ".woff2", ".ttf", ".otf", ".zip", ".pdf"}:
            continue
        files.append(p)
    return files


def test_no_tracked_file_names_the_retired_account():
    """The scan that would have caught the README badge without being told.

    Deliberately repo-wide rather than a list of known offenders. A check that
    only looks where somebody already found a problem cannot find the next one.
    """
    hits = []
    for p in _tracked_text_files():
        # This file has to spell the retired name in order to look for it, so
        # it is the one legitimate mention in the tree. Excluding it by
        # identity rather than by a substring guard, because a guard like
        # "skip lines containing RETIRED_OWNER =" would also silently excuse a
        # real offender that happened to match.
        if p.resolve() == pathlib.Path(__file__).resolve():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if RETIRED_OWNER in line:
                hits.append(f"{p.relative_to(ROOT)}:{n}: {line.strip()[:120]}")

    assert not hits, (
        "these published files still point at the retired "
        f"{RETIRED_OWNER!r} account, whose GitHub username is released and "
        "claimable by a stranger:\n  " + "\n  ".join(hits))


def test_the_package_tells_pypi_where_its_source_lives():
    """A package with no Repository URL is a package with no provenance.

    Libraries.io linked no repo and showed 0 stars for finops-mcp while the
    `nable` shim scored higher, purely because the shim declared a Repository
    key and this did not. Downstream indexes and AI answer engines read this
    metadata to decide whether a package is real and maintained.
    """
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    urls = data["project"]["urls"]

    repo = next((v for k, v in urls.items()
                 if k.lower() in {"repository", "source", "source code"}), None)
    assert repo, (
        "pyproject declares no Repository/Source URL, so PyPI publishes none "
        f"and downstream indexes link no source. Expected {CURRENT_REPO}. "
        f"Declared keys: {sorted(urls)}")
    assert repo.rstrip("/") == CURRENT_REPO, (
        f"the declared source URL is {repo!r}, not the current repo "
        f"{CURRENT_REPO!r}")


def test_every_declared_url_uses_the_current_org():
    """One retired path in project.urls poisons the whole sidebar."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    stale = {k: v for k, v in data["project"]["urls"].items()
             if RETIRED_OWNER in v}
    assert not stale, f"project.urls still names the retired account: {stale}"
