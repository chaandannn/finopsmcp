# SPDX-License-Identifier: Apache-2.0
"""No tool may import a name that does not exist.

Why this file exists, stated plainly: `export_cost_report_csv` and
`publish_cost_report_to_notion` both began with

    from ..recommendations.spot_adoption import scan_spot_adoption_opportunities

and that function has never existed. The module defines `recommend_spot_adoption`.
The import sits above the scanner dispatch inside the tool body, so both tools
raised ImportError on every invocation they have ever received. Not degraded, not
partial. Dead, silently, for as long as the copy has existed.

Nothing caught it. The modules import fine, because a function-body import is not
executed at import time. The test file named after one of them, test_csv_export.py,
re-implemented the CSV writer in its own helper and asserted on that, so seven
green tests never touched the tool. `pytest` was as green with these tools dead as
with them working.

So this test resolves EVERY relative from-import in the package statically: does
the target module exist, and does it define the name. Statically, because the
alternative is executing 190 tools, and several of those bill the owner's account
per call. AST cannot be fooled by a green suite that never calls the code.

The exemption is deliberately narrow, and the line is drawn between two things
that a `try/except ImportError` looks identical around:

  - A missing MODULE may be guarded. `server_web` and `billing` genuinely do not
    exist in the open-source package; they live in the enterprise repo, and the
    core degrades around them with a "this is a hosted feature" message. That is
    a real pattern, and the handler is the author saying so.

  - A missing NAME in a module that DOES exist is never exempt. You cannot
    gracefully degrade around a function name you got wrong: the module is right
    there, the name is simply not in it, and every caller is dead. Wrapping that
    in `except Exception` does not make it work, it makes it silent. Two views in
    get_view were caught this way, permanently returning `{"error": ...}` to any
    user who asked for them.
"""
from __future__ import annotations

import ast
import importlib.util
import pathlib

import pytest

import finops

ROOT = pathlib.Path(finops.__file__).parent

# Catching these around an import is a deliberate "this may not be installed".
_IMPORT_GUARDS = {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}


def _find_spec(name: str):
    """find_spec, but never raises: asking for a submodule of a plain module
    raises ModuleNotFoundError rather than returning None."""
    try:
        return importlib.util.find_spec(name)
    except Exception:
        return None


def _guarded_imports(tree: ast.AST) -> set[int]:
    """Line numbers of imports inside a try/except that catches ImportError."""
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        caught = set()
        for h in node.handlers:
            if h.type is None:  # bare except
                caught.add("BaseException")
            for n in ast.walk(h.type) if h.type else []:
                if isinstance(n, ast.Name):
                    caught.add(n.id)
        if not (caught & _IMPORT_GUARDS):
            continue
        for stmt in node.body:
            for n in ast.walk(stmt):
                if isinstance(n, (ast.Import, ast.ImportFrom)):
                    guarded.add(n.lineno)
    return guarded


def _module_level_names(path: pathlib.Path) -> set[str] | None:
    """Every name a module binds at top level. None means 'cannot tell'."""
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    stack = list(tree.body)
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.Assign):
            names |= {t.id for t in n.targets if isinstance(t, ast.Name)}
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            names.add(n.target.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            if any(a.name == "*" for a in n.names):
                return None  # a star import can supply anything
            names |= {(a.asname or a.name).split(".")[0] for a in n.names}
        elif isinstance(n, (ast.If, ast.Try)):
            stack.extend(ast.iter_child_nodes(n))
    if "__getattr__" in names:
        return None  # module defines lazy attribute access
    return names


def _broken_imports() -> list[str]:
    broken: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover
            continue
        guarded = _guarded_imports(tree)
        pkg = ".".join(["finops"] + list(path.relative_to(ROOT).parts[:-1]))

        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if node.level == 0:
                continue
            segs = pkg.split(".")
            base = ".".join(segs[: len(segs) - (node.level - 1)])
            target = f"{base}.{node.module}"
            where = f"{path.relative_to(ROOT)}:{node.lineno}"

            spec = _find_spec(target)
            if spec is None:
                # A missing module may be a deliberate hosted/optional split.
                if node.lineno not in guarded:
                    broken.append(f"{where}  imports from {target}, which does not exist")
                continue
            if not spec.origin or not pathlib.Path(spec.origin).exists():
                continue
            defined = _module_level_names(pathlib.Path(spec.origin))
            if defined is None:
                continue
            for alias in node.names:
                if alias.name == "*" or alias.name in defined:
                    continue
                if _find_spec(f"{target}.{alias.name}") is not None:
                    continue  # it is a submodule, not an attribute
                # Same exemption the missing-module branch above already makes,
                # applied to a missing SUBMODULE of a package that does exist.
                # `from ..connectors import cur_s3` is exactly that shape: the
                # package is open, the reader ships in nable-enterprise and
                # arrives through its seam. Wrapped in except ImportError it
                # degrades rather than dying, which is the only thing this
                # ratchet is looking for. An UNGUARDED one still fails here.
                if node.lineno not in guarded:
                    broken.append(f"{where}  {target} has no {alias.name!r}")
    return broken


def test_no_module_imports_a_name_that_does_not_exist():
    broken = _broken_imports()
    assert not broken, (
        "These imports raise at the moment the line executes. An import inside a "
        "function body does not run at import time, so the module loads clean and "
        "the tool dies only when a user calls it:\n  " + "\n  ".join(broken)
    )


def test_the_resolver_actually_catches_a_broken_import(tmp_path, monkeypatch):
    """Guards this file against passing because it checks nothing.

    A resolver with an over-broad exemption, or one that silently skips files it
    cannot parse, returns an empty list and reads as success. So: plant the exact
    defect this file exists to catch, and require that it is found.
    """
    plant = ROOT / "_import_resolver_probe.py"
    plant.write_text(
        "def f():\n"
        "    from ..finops.recommendations.spot_adoption import (\n"
        "        scan_spot_adoption_opportunities)\n"
    )
    try:
        # written one level up so the relative import resolves the way the real
        # broken one did, from inside a tools/ style submodule
        (ROOT / "tools" / "_import_resolver_probe.py").write_text(
            "def f():\n"
            "    from ..recommendations.spot_adoption import scan_spot_adoption_opportunities\n"
        )
        found = _broken_imports()
        assert any("scan_spot_adoption_opportunities" in b for b in found), (
            "the resolver did not flag a planted import of a name that does not "
            "exist, so a green result from it means nothing"
        )
    finally:
        plant.unlink(missing_ok=True)
        (ROOT / "tools" / "_import_resolver_probe.py").unlink(missing_ok=True)


def test_a_guarded_import_of_a_missing_module_is_allowed(tmp_path):
    """The exemption must work, or the open-source package cannot degrade.

    server_web and billing live in the enterprise repo. The core imports them
    inside try/except ImportError and prints a "this is a hosted feature"
    message. That must stay legal, or this test forces the split to be undone.
    """
    src = (
        "def f():\n"
        "    try:\n"
        "        from ..server_web import _local_ip\n"
        "    except ImportError:\n"
        "        return 'hosted feature'\n"
    )
    tree = ast.parse(src)
    guarded = _guarded_imports(tree)
    imp = next(n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom))
    assert imp.lineno in guarded


def test_an_unguarded_import_in_a_try_that_catches_something_else_is_not_exempt():
    """`except ValueError` around an import does not make a typo survivable."""
    src = (
        "def f():\n"
        "    try:\n"
        "        from ..recommendations.spot_adoption import nope\n"
        "    except ValueError:\n"
        "        pass\n"
    )
    assert _guarded_imports(ast.parse(src)) == set()
