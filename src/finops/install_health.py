"""Is this install actually complete?

Field telemetry, 2026-08-08: four machines on 0.8.207-0.8.209, Linux, system
Python 3.12, every scan failing with `missing_dep` and boto3 reporting `absent`.
The published wheel is fine (`Requires-Dist: boto3>=1.34.0` is there, verified
against PyPI), so those environments installed the package WITHOUT its
dependencies: `--no-deps`, a pruned container layer, or a copy of site-packages
that did not bring everything along.

The bad part was not the failure, it was the timing. A dependency-less install
starts the MCP server, advertises every tool, and answers `tools/list` looking
perfectly healthy. Nothing says otherwise until a cloud tool is finally called
and dies. So the first honest signal arrived minutes into someone's session,
inside a scan, instead of at the moment they could have fixed it in one command.

This module answers the question early and precisely: which core runtime
dependencies are declared but not importable. It reads the installed metadata
rather than a hardcoded list, so a dependency added to pyproject is covered
without touching this file.

Import-safe by construction: it imports nothing heavy and never raises. A
diagnostic that can crash is worse than no diagnostic.

`install_shape()` was added 2026-08-14 for a reason worth writing down. This
module's own docstring above names three possible causes and cannot choose
between them, and neither could I: 13 `missing_dep` events across 0.8.207-0.8.209
all said `boto3: absent` and nothing about how that environment came to be. I
spent an afternoon eliminating candidates by hand (`uvx nable scan` from a clean
HOME installs botocore correctly, the wheel declares boto3, CI runs no scans)
and still could not name the path. `install_shape()` makes the next one a single
query. It reports a CATEGORY, never a path: "uv", "venv", "system", plus whether
the code is imported from site-packages or a checkout. That distinction is the
whole game, because "a stranger cloned the repo and ran it" and "a container
layer got pruned" need completely different fixes.
"""
from __future__ import annotations

import importlib.util
import os
import platform
import sys

# Distribution name -> the module you actually import. The bar is deliberately
# high: only packages whose absence breaks a whole capability area AND that have
# no fallback. A missing optional extra is an unused feature, not a broken
# install, and crying "broken" over a survivable gap trains people to ignore this.
#
# keyring is deliberately NOT here. The vault and trial store have been
# file-first since 3b36e3c, exactly so a missing, locked or hostile OS keychain
# degrades instead of failing, and this test suite stubs it out. Listing it would
# report a healthy install as broken.
_CORE: dict[str, str] = {
    "boto3": "boto3",
    "botocore": "botocore",
    "mcp": "mcp",
    "pydantic": "pydantic",
    "sqlalchemy": "sqlalchemy",
    "httpx": "httpx",
    "pyyaml": "yaml",
    "cryptography": "cryptography",   # Fernet: the vault cannot encrypt without it
}


def _importable(module: str) -> bool:
    """True if `module` can be found. find_spec, not import: importing pulls the
    package into memory and can execute arbitrary module-level code, which a
    health check has no business doing."""
    try:
        return importlib.util.find_spec(module) is not None
    except BaseException:
        # A broken meta-path finder, or a package whose parent raises on import,
        # both mean "cannot be relied on", which is the answer we want.
        return False


def missing_core_dependencies() -> list[str]:
    """Declared-and-required packages that are not importable here."""
    return sorted(dist for dist, mod in _CORE.items() if not _importable(mod))


def _env_kind() -> str:
    """Which package manager built this interpreter's environment.

    Coarse on purpose. The question being answered is "what kind of install
    skipped the dependencies", and for that "uv" vs "venv" vs "system" is the
    whole useful resolution. Anything finer starts encoding someone's directory
    layout, which this must never transmit.
    """
    if os.environ.get("CONDA_PREFIX"):
        return "conda"
    if not (hasattr(sys, "real_prefix") or sys.prefix != sys.base_prefix):
        return "system"
    # A venv, so ask what made it. uv stamps pyvenv.cfg; pipx does not, but its
    # venvs sit under a pipx-owned root, and PIPX_HOME is set when it is running.
    try:
        with open(os.path.join(sys.prefix, "pyvenv.cfg"), encoding="utf-8") as fh:
            cfg = fh.read()
        if any(line.split("=")[0].strip() == "uv" for line in cfg.splitlines()):
            return "uv"
    except BaseException:
        pass
    if os.environ.get("PIPX_HOME") or f"{os.sep}pipx{os.sep}" in sys.prefix:
        return "pipx"
    return "venv"


def _layout() -> str:
    """Is this package imported from an installed wheel, or from a checkout?

    "source" means somebody cloned the repo and ran it in place. That install
    never had a dependency resolution step at all, so a missing boto3 is
    expected rather than mysterious, and the fix is `pip install -e .` and not
    a reinstall. Worth one word to tell those two worlds apart.
    """
    try:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if os.path.exists(os.path.join(os.path.dirname(here), "pyproject.toml")):
            return "source"
        return "site-packages" if "site-packages" in __file__ else "other"
    except BaseException:
        return "unknown"


def _containerised() -> bool:
    try:
        return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")
    except BaseException:
        return False


def install_shape() -> dict:
    """How this install was built, as categories safe to send.

    No paths, no usernames, no hostnames: a package-manager name, a layout word,
    a bool, a two-part Python version and an OS name. Every value is drawn from
    a closed set this file defines.
    """
    try:
        return {
            "env_kind": _env_kind(),
            "layout": _layout(),
            "container": _containerised(),
            "py": f"{sys.version_info.major}.{sys.version_info.minor}",
            "os": platform.system(),
        }
    except BaseException:
        return {}


def install_health() -> dict:
    """A structured verdict on this install.

    `ok` False means the package is present but its dependencies are not, which
    is a broken install rather than a missing feature, and the fix is one
    command. The message is written to be shown verbatim.
    """
    missing = missing_core_dependencies()
    if not missing:
        return {"ok": True, "missing": []}
    return {
        "ok": False,
        "missing": missing,
        "reason": "installed without dependencies",
        "detail": (
            f"finops-mcp is installed but {len(missing)} required package(s) are "
            f"missing: {', '.join(missing)}. The published wheel declares them, so "
            "this environment installed the package without its dependencies "
            "(pip --no-deps, a pruned container layer, or a partial copy of "
            "site-packages)."
        ),
        "fix": "pip install --upgrade --force-reinstall finops-mcp",
        "isolated_fix": "uvx --python 3.12 nable",
    }
