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
"""
from __future__ import annotations

import importlib.util

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
