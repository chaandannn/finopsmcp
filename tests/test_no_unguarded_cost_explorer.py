"""No NEW module may reach Cost Explorer without saying who it is for.

Cost Explorer bills PER REQUEST against the customer's own account, so the rule
is that no unattended path calls it. The rule was enforced by everyone
remembering, and llm_costs.py did not: it made two CE calls, job_ai_monitor ran
it nightly at 05:00, and every customer was charged for a report they never
asked for.

A rule you have to remember is not a rule.

This is a BASELINE ratchet, not a clean-bill-of-health. The modules below build a
Cost Explorer client without consulting the guard TODAY. Most are reachable only
when a person asked, which is exactly what Cost Explorer is for, but that has not
been verified module by module and this test does not claim it has. What it does
is stop the set from GROWING: a new unguarded caller fails here, and the person
adding it has to say which case it is.

Shrinking this list is real work and worth doing. Adding to it should not be
possible by accident.
"""
from __future__ import annotations

import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "finops"

# Unverified debt, not an approval. See the module docstring.
KNOWN_UNGUARDED = {
    "anomaly/detector.py",
    "attribution/fetcher.py",
    "connectors/aws_org.py",
    "connectors/aws_services/bedrock.py",
    "connectors/aws_services/documentdb.py",
    "connectors/aws_services/marketplace.py",
    "connectors/aws_services/textract.py",
    "connectors/kubernetes_costs.py",
    "connectors/universal.py",
    "doctor.py",
    "ml/forecasting.py",
    "recommendations/bedrock_routing.py",
    "recommendations/commitments.py",
    "recommendations/database_savings_plans.py",
    "recommendations/genuine_savings.py",
    "recommendations/rate_detector.py",
    "recommendations/textract_env.py",
    "security/iam_setup.py",
    "setup_wizard.py",
    "tools/aws.py",
}

_RAW_CE = re.compile(r'''boto3\.client\(\s*["\']ce["\']''')


def _unguarded() -> set[str]:
    out = set()
    for py in SRC.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        # A mention in a comment is not a call. The first version of this counted
        # them and reported eighteen files, which is how a ratchet gets disabled.
        hits = [ln for ln in text.splitlines()
                if _RAW_CE.search(ln) and not ln.strip().startswith("#")]
        if not hits:
            continue
        if "cost_explorer_allowed" in text or "should_use_cost_explorer" in text:
            continue
        out.add(py.relative_to(SRC).as_posix())
    return out


def test_no_new_module_reaches_cost_explorer_unguarded():
    new = sorted(_unguarded() - KNOWN_UNGUARDED)
    assert not new, (
        f"{new} build a Cost Explorer client and never consult the guard. CE "
        "bills the customer per request, so an unattended caller charges them "
        "for work they did not ask for. Call cost_explorer_allowed() first, or "
        "add it to KNOWN_UNGUARDED with a reason no scheduled job reaches it.")


def test_the_baseline_does_not_list_modules_that_are_already_clean():
    """A stale allowlist hides the next regression behind a name that no longer
    needs to be there."""
    stale = sorted(KNOWN_UNGUARDED - _unguarded())
    assert not stale, (
        f"{stale} now consult the guard; remove them from KNOWN_UNGUARDED so it "
        "keeps describing the real debt")
