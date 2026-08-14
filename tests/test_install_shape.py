# SPDX-License-Identifier: Apache-2.0
"""What install_shape() may say, and what it must never say.

This exists because of a specific failure of instrumentation. Between
0.8.207 and 0.8.209, thirteen scans across several machines failed with
`missing_dep` and `boto3: absent`. install_health.py's own docstring lists three
possible causes (pip --no-deps, a pruned container layer, a partial copy of
site-packages) and the telemetry could not distinguish them. Neither could I by
hand: `uvx nable scan` from a clean HOME installs botocore correctly, the
published wheel declares boto3, and no CI job runs a scan. The cause stayed
un-nameable, so nothing could be fixed.

install_shape() closes that. Which makes it a new way to leak, so the second
half of this file is about the values it must never emit. The rule is that every
field comes from a closed set defined in install_health.py. A path, a username or
a hostname reaching PostHog would be a worse bug than the one this solves,
because nable's whole claim is that it does not send your environment anywhere.
"""
from __future__ import annotations

import json
import os
import re
import sys

import pytest

from finops.install_health import install_shape


ENV_KINDS = {"conda", "system", "uv", "pipx", "venv"}
LAYOUTS = {"source", "site-packages", "other", "unknown"}


def test_it_describes_the_environment_it_is_actually_running_in():
    shape = install_shape()
    assert shape["env_kind"] in ENV_KINDS, shape
    assert shape["layout"] in LAYOUTS, shape
    assert isinstance(shape["container"], bool)
    assert shape["py"] == f"{sys.version_info.major}.{sys.version_info.minor}"
    assert shape["os"] in {"Darwin", "Linux", "Windows"}


def test_this_repo_reads_as_a_venv_running_from_source():
    """The suite runs from .venv against src/, so the answer is knowable here.

    A test that only checks "the value is in the allowed set" would pass if the
    function returned a constant. This pins the two fields against a ground
    truth the test runner can verify for itself.
    """
    shape = install_shape()
    in_venv = sys.prefix != sys.base_prefix
    assert (shape["env_kind"] != "system") == in_venv, (
        f"env_kind={shape['env_kind']} but sys.prefix "
        f"{'!=' if in_venv else '=='} sys.base_prefix"
    )
    import finops
    from_checkout = os.path.exists(
        os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(finops.__file__)))), "pyproject.toml"))
    if from_checkout:
        assert shape["layout"] == "source", (
            "finops is being imported from a checkout but layout does not say so. "
            "That distinction decides the advice: a checkout needs `pip install "
            "-e .`, an installed wheel needs --force-reinstall"
        )


# ── the half that matters more ────────────────────────────────────────────────

def test_it_leaks_no_path_username_or_hostname():
    """Every value, scanned for anything that identifies this machine.

    Checked against the real values on this box rather than a regex for
    "looks like a path", because the failure mode is a field that happens to
    contain $HOME on someone else's setup and not on the author's.
    """
    blob = json.dumps(install_shape())

    secrets = {
        "home": os.path.expanduser("~"),
        "cwd": os.getcwd(),
        "sys.prefix": sys.prefix,
        "sys.executable": sys.executable,
        "user": os.environ.get("USER") or os.environ.get("USERNAME") or "",
    }
    for label, value in secrets.items():
        if value and len(value) > 3:
            assert value not in blob, f"install_shape() leaked {label}: {value!r}"

    assert os.sep not in blob.replace("\\/", ""), (
        f"a path separator reached the payload: {blob}"
    )
    assert not re.search(r"[A-Za-z]:\\", blob), f"a Windows drive path leaked: {blob}"


def test_every_value_comes_from_a_closed_set():
    """No free-form strings. A value read off the machine is a value that can
    carry something off the machine."""
    shape = install_shape()
    assert set(shape) == {"env_kind", "layout", "container", "py", "os"}, (
        "a field was added to install_shape without being reviewed here. Every "
        "new field is a new thing sent to PostHog on a failed scan"
    )
    assert re.fullmatch(r"\d+\.\d+", shape["py"]), (
        f"py={shape['py']!r}: send major.minor only, never the full version "
        "string, which on some builds carries a compiler and a build date"
    )


def test_it_never_raises_even_when_the_environment_is_hostile():
    """It runs inside an except: block on a path that is already failing.

    If a diagnostic raises there it replaces a useful error message with a
    traceback, which is strictly worse than not having the diagnostic.
    """
    import finops.install_health as ih

    for victim in ("_env_kind", "_layout", "_containerised"):
        original = getattr(ih, victim)
        setattr(ih, victim, lambda: (_ for _ in ()).throw(OSError("permission denied")))
        try:
            assert install_shape() == {}, (
                f"{victim} blew up and install_shape did not absorb it"
            )
        finally:
            setattr(ih, victim, original)


def test_the_failing_scan_actually_reports_it():
    """The wiring. A shape nobody sends answers no questions.

    Deleting the install_shape() call from cli_scan leaves every test above
    passing, which is exactly the shape of bug this repo keeps finding: the
    helper is tested, the call site is not.
    """
    import ast
    import inspect

    import finops.cli_scan as cli_scan

    fn = next(
        n for n in ast.walk(ast.parse(inspect.getsource(cli_scan)))
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "run"
    )
    body = ast.unparse(fn)
    assert "install_shape" in body, (
        "cli_scan.run never calls install_shape, so a missing_dep event still "
        "arrives saying only that boto3 is absent, which is the exact dead end "
        "this was written to end"
    )
    assert "deps.update" in body, (
        "install_shape is called but its result is not merged into the props "
        "handed to _fail, so it is computed and thrown away"
    )
