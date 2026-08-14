# SPDX-License-Identifier: Apache-2.0
"""A caught exception must not become a dollar figure. Ratchet, not a rule.

Why this file exists, stated plainly: one defect shape accounted for more of this
codebase's wrong numbers than any other, and fixing instances of it did not stop
it recurring.

    except Exception:
        return 0.0

Every one of these reads as a fact. Not "we could not look", but "it is zero",
and zero is never the neutral answer for a cost tool. It is the most alarming
reading available:

  - a CloudWatch metric that threw became 0.000 GB/day, so every NAT gateway in
    the region was reported idle the moment CloudWatch was unreachable
  - a NetworkOut read that threw became 0.0 bytes, which fell straight into the
    guard that spares network-bound hosts, so the failure DISABLED the check
    that would have saved a Kafka broker from a terminate suggestion
  - a DENIED Savings Plans coverage call became 0% coverage, and 0% coverage is
    the trigger for a purchase, so a missing IAM permission produced advice to
    commit $5,940 a month
  - a failed snapshot query became a $0 week, posted to a team's Slack as
    "Weekly cost: $0 (+0.0% vs last week)" and reported as sent successfully

Six of these were fixed by hand. That is the problem: they were fixed as
instances. A rule in a document ("don't return 0.0 on error") does not fail
anything, and the seventh gets written next week.

So this is a ceiling that can only come down. The allowlist below is the exact
set that existed when it was written. A new one fails immediately. Fixing one and
leaving its entry here ALSO fails, so the list cannot rot into a permanent
exemption: it has to shrink as the work gets done, and when it is empty this file
becomes a plain prohibition.

Keyed on (file, name), never on line numbers. A test in this repo already pinned
a fix by line number once, and a mutation that moved the code one line up
satisfied it while breaking the property.

Scope note: only zeros that flow into money or a percentage are counted. A
counter defaulting to 0 after a failed listing is fine and there are 39 of those;
they are not what puts a wrong figure in front of a customer.
"""
from __future__ import annotations

import ast
import pathlib
import re

import finops

ROOT = pathlib.Path(finops.__file__).parent

# A name that reads as money or a rate. Deliberately broad: a false positive
# costs one allowlist line, a false negative costs a customer a wrong number.
_MONEYISH = re.compile(
    r"(cost|usd|total|amount|spend|savings|pct|percent|coverage|rate|price|waste|budget)",
    re.I,
)

# The sites that existed when this ratchet was installed. Each is a place where a
# failed read still becomes a figure. This list may only get shorter.
#
# Remove an entry in the same commit that fixes its site: a strict ratchet fails
# on stale entries too, so leaving one behind is caught rather than forgotten.
KNOWN: set[tuple[str, str]] = {
    ("analytics/ai_kpis.py", "input_price"),
    ("focus/translators/generic.py", "_amount"),
    ("focus/translators/llm.py", "amount"),
    ("recommendations/commitments.py", "_ec2_spend_for_tag"),
    ("recommendations/commitments.py", "_total_ec2_spend"),
    ("recommendations/database_savings_plans.py", "_get_database_sp_coverage"),
    ("recommendations/lambda_snapstart.py", "_get_pc_monthly_cost"),
    ("recommendations/learning/rescorer.py", "savings"),
    ("recommendations/textract_env.py", "_get_total_textract_spend"),
    ("scheduler/jobs.py", "open_savings"),
    ("scheduler/jobs.py", "verified_savings"),
    ("server.py", "_savings_found_monthly"),
    ("server.py", "usd_delta"),
    ("tools/notifications.py", "open_savings"),
    ("tools/notifications.py", "verified_savings"),
}


def _zero_constant(node: ast.AST) -> bool:
    return (isinstance(node, ast.Constant)
            and isinstance(node.value, (int, float))
            and not isinstance(node.value, bool))


def _enclosing_function(tree: ast.AST, lineno: int) -> str:
    """Innermost function containing lineno, for naming a bare `return 0.0`."""
    best, best_span = "<module>", None
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = n.end_lineno or n.lineno
            if n.lineno <= lineno <= end:
                span = end - n.lineno
                if best_span is None or span < best_span:
                    best, best_span = n.name, span
    return best


def find_money_zeros() -> set[tuple[str, str]]:
    """Every (file, name) where an except handler yields a numeric money value."""
    found: set[tuple[str, str]] = set()
    for path in sorted(ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover
            continue
        rel = path.relative_to(ROOT).as_posix()
        for handler in ast.walk(tree):
            if not isinstance(handler, ast.ExceptHandler):
                continue
            for n in ast.walk(handler):
                names: list[str] = []
                if isinstance(n, ast.Return) and _zero_constant(n.value):
                    names = [_enclosing_function(tree, n.lineno)]
                elif isinstance(n, ast.Assign):
                    value = n.value
                    ok = _zero_constant(value) or (
                        isinstance(value, ast.Tuple)
                        and bool(value.elts)
                        and all(_zero_constant(e) for e in value.elts)
                    )
                    if not ok:
                        continue
                    names = [t.id for t in n.targets if isinstance(t, ast.Name)]
                for name in names:
                    if _MONEYISH.search(name):
                        found.add((rel, name))
    return found


def test_the_detector_is_not_silently_finding_nothing():
    """Guards the ratchet against passing because it inspects an empty set.

    A scanner that quietly stops matching turns this whole file into a green
    no-op, which is the exact failure mode it was built to prevent elsewhere.
    """
    found = find_money_zeros()
    assert len(found) >= 5, (
        f"the detector found only {len(found)} sites. Either this codebase got "
        f"remarkably clean, or the AST walk stopped matching and this file is "
        f"now asserting nothing."
    )


def test_no_new_place_turns_a_failed_read_into_a_dollar_figure():
    """The ceiling. New code may not add one."""
    new = sorted(find_money_zeros() - KNOWN)
    assert not new, (
        "these turn a caught exception into a money or percent figure, which "
        "publishes a number nobody read:\n  "
        + "\n  ".join(f"{f}  ->  {n}" for f, n in new)
        + "\n\nReturn None and let the caller say 'unavailable'. Unknown and zero "
          "are different facts, and for a cost tool zero is the alarming one: a "
          "denied coverage call reading as 0% is what recommended a $5,940/mo "
          "purchase off a permissions error."
    )


def test_the_allowlist_has_no_stale_entries():
    """The pawl. A fixed site must leave the list in the same commit.

    Without this the allowlist becomes a permanent exemption: entries accumulate,
    nobody prunes them, and the ceiling stops meaning anything. Failing on stale
    entries is what makes the count go down instead of sideways.
    """
    stale = sorted(KNOWN - find_money_zeros())
    assert not stale, (
        "these are in KNOWN but no longer match, so they were fixed and the "
        "entry was left behind. Delete them from KNOWN:\n  "
        + "\n  ".join(f"{f}  ->  {n}" for f, n in stale)
    )


def test_the_fixed_sites_stay_fixed():
    """The ones already corrected must not regress into the allowlist.

    _savings_plan_coverage is the sharpest example: it returned 0.0 on a denied
    call and that 0% was read as "nothing is covered", which is the condition
    that recommends a purchase. It returns None now, and its sibling
    _ri_coverage in the same file is still on the list above, which is precisely
    why a ratchet is more useful here than a fixed instance.
    """
    import inspect

    from finops.recommendations import commitments

    src = inspect.getsource(commitments._savings_plan_coverage)
    handler = src[src.index("except "):]
    assert "return None" in handler, (
        "_savings_plan_coverage went back to returning a number when the "
        "coverage call fails. A denied read must not become 0% coverage"
    )
    assert not re.search(r"return\s+0(\.0)?\b", handler)
