# SPDX-License-Identifier: Apache-2.0
"""A shim fix that never reached PyPI is a shim fix that did not happen.

Measured 2026-08-19 by running the exact scenario a reviewer flagged on a
directory submission:

    $ uvx --python 3.10 nable --version
    nable needs Python 3.11 or newer. This is Python 3.10.
    Run this instead: uvx --python 3.12 nable
    exit 1

That message was supposed to be gone. nable_shim.py in this repo re-execs
itself under a uv-managed 3.12 rather than asking the user to retype the
command, and the Ctrl-C handler that keeps a traceback off that path merged in
PR #108. None of it reached anyone. shim/pyproject.toml still declared 0.1.3,
which was already on PyPI, so there was no version left to publish under. The
published artifact contains zero occurrences of _reexec_under_managed_python.
This repo contains two.

The failure is silent by construction. Publishing fires on a shim-v* tag,
nobody pushed one, and a manual run would have hit skip-existing and reported
success while uploading nothing.

Fourth instance of one shape in a week: the SBOM described an eleven-week-old
release, the GitHub Releases feed froze at one entry against 208 on PyPI, the
MCP registry carried a duplicate 36 releases behind, and now the shim. All four
looked healthy because nothing 404s.

So the lock below is deliberately annoying. Edit nable_shim.py and this fails
until the version moves and the digest is updated, which is the moment to
remember the tag. No network, so it fails in CI exactly as it fails locally.
"""
from __future__ import annotations

import hashlib
import pathlib
import re

SHIM = pathlib.Path(__file__).resolve().parents[1] / "shim"

# Bump BOTH when nable_shim.py changes, then push a shim-v<version> tag.
PUBLISHED_UNDER = "0.1.4"
SOURCE_SHA256 = "85a33fb6f03918f4fd43153d4f64e813faf13b15f09578e89d48d78c9c0e19a9"


def _declared_version() -> str:
    return re.search(r'^version = "([^"]+)"',
                     (SHIM / "pyproject.toml").read_text(), re.M).group(1)


def test_the_shim_source_matches_the_version_it_will_publish_under():
    actual = hashlib.sha256((SHIM / "nable_shim.py").read_bytes()).hexdigest()
    version = _declared_version()

    if actual != SOURCE_SHA256:
        raise AssertionError(
            "shim/nable_shim.py changed since it was last locked.\n"
            f"  locked digest : {SOURCE_SHA256}\n"
            f"  actual digest : {actual}\n"
            f"  declared vers : {version}\n\n"
            "Publishing it takes three steps, and skipping any one ships "
            "nothing while looking fine:\n"
            "  1. raise version in shim/pyproject.toml\n"
            "  2. update PUBLISHED_UNDER and SOURCE_SHA256 here\n"
            "  3. push a shim-v<version> tag, which is what actually publishes\n\n"
            "Step 3 is the one that was missed. The previous edit sat "
            "unpublished under a version already taken on PyPI.")

    assert version == PUBLISHED_UNDER, (
        f"shim/pyproject.toml declares {version} but this lock says "
        f"{PUBLISHED_UNDER}. They have to agree, or the digest is vouching for "
        "a version nobody intends to publish.")


def test_the_behaviour_the_floor_lesson_depends_on_is_still_there():
    """Guards what the shim does, not only that its bytes are unchanged.

    A digest notices any edit but cannot tell a comment fix from someone
    deleting the re-exec, which is the specific change that turns uvx nable
    back into a dead end on an old interpreter.
    """
    src = (SHIM / "nable_shim.py").read_text()

    assert "_reexec_under_managed_python" in src, (
        "the shim no longer re-execs under a managed interpreter, so uvx nable "
        "on Python 3.10 is back to printing a command and exiting 1")
    assert "KeyboardInterrupt" in src, (
        "Ctrl-C during the interpreter fetch prints the traceback this module "
        "exists to prevent (PR #108)")
