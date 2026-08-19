# SPDX-License-Identifier: Apache-2.0
"""Somebody else's deprecation notice must not land on the value moment.

Measured 2026-08-19 from a screenshot of a real first run through `uvx nable`:
pydantic-settings 2.15.0 emits IncompleteFieldDefinitionWarning about FastMCP's
`lifespan` field, and it printed in the middle of "Scanning your account",
immediately above the first real numbers. Six lines of warning about an
unresolved forward reference, telling the reader to call model_rebuild() on a
model nobody in the room owns.

It matters more than its size. PostHog puts the biggest drop in the funnel at
ran-the-tool to first-command, so the seconds around that first output are the
ones that decide whether somebody stays.

The reason this shipped is the trap worth remembering: the development venv had
pydantic-settings 2.14.2, which does not warn, while `uvx` resolves 2.15.0,
which does. The defect was invisible to every local run and unavoidable for
every new user. Same shape as the venv that was silently on 3.10.5 and the
boto3 skew that killed 29 of 30 field scans.

That is also why the assertions below are written the way they are. On an
interpreter with 2.14.2 installed, "assert no warning appears" passes whether
or not the filter exists, which is a test that cannot fail. So one test checks
the filter is wired into main(), and the other proves it suppresses a warning
manufactured to look like the real one, independent of what is installed.
"""
from __future__ import annotations

import ast
import inspect
import textwrap
import warnings

from finops import entry


def test_the_filter_is_narrow_enough_to_keep_our_own_warnings():
    """A blanket ignore would also hide the warnings we rely on.

    ResourceWarning catches leaked sockets and DeprecationWarning is how our
    own code announces a retirement. Silencing everything to hide one upstream
    message would trade a cosmetic problem for a diagnostic one.
    """
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        entry._quiet_upstream_import_noise()

        warnings.warn("ours, keep it", DeprecationWarning)
        warnings.warn("a leaked socket", ResourceWarning)
        warnings.warn("some other library", UserWarning)

    kinds = {w.category.__name__ for w in seen}
    assert "DeprecationWarning" in kinds, "our own deprecations got swallowed"
    assert "ResourceWarning" in kinds, "leaked-resource warnings got swallowed"
    assert "UserWarning" in kinds, (
        "every UserWarning is being dropped, not just the pydantic_settings "
        "one; the filter lost its module restriction")


def test_the_pydantic_settings_warning_is_suppressed():
    """Manufactured to match the real one, so the result does not depend on
    which pydantic-settings happens to be installed on the machine running
    the suite. The dev venv carries 2.14.2 and never emits it; uvx resolves
    2.15.0 and always does.
    """
    entry._quiet_upstream_import_noise()

    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        entry._quiet_upstream_import_noise()
        # __warningregistry__ lookups key off the caller's module name, which
        # is what the filter matches on, so name the frame accordingly.
        exec(  # noqa: S102
            "import warnings; warnings.warn("
            "'Field \\'lifespan\\' has an incomplete definition', UserWarning)",
            {"__name__": "pydantic_settings.sources.utils"},
        )

    offending = [w for w in seen if "incomplete definition" in str(w.message)]
    assert not offending, (
        "the pydantic-settings warning still reaches the terminal during a "
        f"scan: {[str(w.message) for w in offending]}")


def test_main_installs_the_filter_before_it_dispatches():
    """Order is the whole fix.

    The warning fires at `import finops.server`, which happens somewhere
    downstream of the dispatch. A filter installed after that import is a
    filter that does nothing, and the terminal output would look identical to
    having no filter at all.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(entry.main)))
    body = tree.body[0].body

    calls = [
        i for i, node in enumerate(body)
        if any(isinstance(n, ast.Call) and getattr(n.func, "id", "")
               == "_quiet_upstream_import_noise" for n in ast.walk(node))
    ]
    assert calls, (
        "main() no longer calls _quiet_upstream_import_noise, so the upstream "
        "warning is back on the first screen a new user sees")

    dispatches = [
        i for i, node in enumerate(body)
        if any(isinstance(n, (ast.Import, ast.ImportFrom))
               for n in ast.walk(node))
    ]
    assert dispatches, "main() no longer imports anything; this test is stale"
    assert min(calls) < min(dispatches), (
        "the filter is installed after main() starts importing, and the "
        "warning fires during those imports, so it arrives too late")
