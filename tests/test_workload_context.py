# SPDX-License-Identifier: Apache-2.0
"""A pull request against somebody's sandbox is worse than no pull request.

Chandan asked the right question on 2026-08-16: "how do we make sure we're not
just opening pull requests at every spike, but have some context to say this is
an actual anomaly, this is normal, this is a sandbox workload." The answer was
that nothing in the product read workload context at all. A load test someone is
deliberately running and a production fleet quietly burning money look identical
to a statistical detector: both are spend above a baseline.

The safety argument is an asymmetry, and it is the only thing that lets this ship
enabled by default:

    positive evidence of non-production  ->  hold the action back
    anything else, including unknown     ->  behave exactly as before

An untagged estate is therefore never worse off than it was. The tests below pin
that asymmetry, the ranking between disagreeing signals, and the call site, since
a classifier nothing calls is a classifier that does nothing.
"""
from __future__ import annotations

import json

import pytest

from finops.context.workload import WorkloadContext, classify, suppression_note


# ── the asymmetry ────────────────────────────────────────────────────────────

def test_no_signal_is_unknown_and_unknown_never_suppresses():
    """The load-bearing case. Most estates are not tagged.

    If "we could not tell" collapsed into "non-production", the feature would
    silently switch off rightsizing for every customer who never adopted a
    tagging policy, which is most of the ones who need it.
    """
    ctx = classify(tags={}, account_name="481516234203", resource_name="i-0abc123")

    assert ctx.kind == "unknown"
    assert ctx.is_nonprod is False, (
        "an unclassified resource must behave exactly as it did before this "
        "module existed; suppressing on absence of evidence turns a safety "
        "feature into a silent outage of the product")


def test_unknown_is_kept_distinct_from_production():
    """"No signal" and "production" are different facts and must stay different.

    Reporting a guess as a fact is how a failed read becomes a number. A caller
    that wants to say "we checked and it is production" has to be able to tell
    that apart from "nothing said".
    """
    assert classify().kind == "unknown"
    assert classify(tags={"Environment": "production"}).kind == "prod"


@pytest.mark.parametrize("value", [
    "sandbox", "sbx", "dev", "development", "staging", "test", "qa", "uat",
    "nonprod", "non-prod", "preprod", "demo", "scratch", "playground",
])
def test_the_words_teams_actually_use_for_not_production(value):
    ctx = classify(tags={"Environment": value})
    assert ctx.is_nonprod, f"{value!r} did not read as non-production"
    assert value in ctx.reason(), "the evidence has to quote what it matched"


@pytest.mark.parametrize("value", ["production", "prod", "prd", "live"])
def test_production_words_are_not_suppressed(value):
    assert classify(tags={"Environment": value}).is_nonprod is False


def test_preprod_is_not_read_as_prod():
    """The substring trap. "preprod" contains "prod" and is the opposite of it."""
    assert classify(tags={"Environment": "preprod"}).kind == "nonprod"
    assert classify(tags={"env": "pre-prod"}).kind == "nonprod"


def test_a_name_that_says_both_things_says_nothing():
    """"development-team-prod-api": production owned by the dev team, or a dev box?

    The string does not say, and word order is not a specification. This one
    failed on the first run asserting "prod", which was me picking a reading and
    calling it a fact. Cancelling is the honest answer: it defers to a stronger
    signal when one exists, and otherwise lands on unknown, which changes nobody's
    behaviour.
    """
    assert classify(resource_name="development-team-prod-api").kind == "unknown"

    # A real tag settles what the name could not.
    ctx = classify(tags={"Environment": "dev"}, resource_name="development-team-prod-api")
    assert ctx.kind == "nonprod", ctx.reason()


def test_a_word_inside_another_word_is_not_a_signal():
    """Substring matching would read "prod" out of "reproducer" and flip a box."""
    assert classify(resource_name="api-reproducer-7").kind == "unknown"
    assert classify(resource_name="devops-platform").kind == "unknown"


# ── which signal wins ────────────────────────────────────────────────────────

def test_an_explicit_tag_beats_a_naming_habit():
    """A tag is a statement of intent. A name is how somebody typed it once."""
    ctx = classify(tags={"Environment": "production"}, resource_name="acme-dev-api-7")

    assert ctx.kind == "prod"
    assert "tag Environment" in ctx.reason()


def test_when_equal_signals_disagree_the_cautious_one_wins():
    """Account says sandbox, namespace says prod, same weight.

    Holding back a pull request on a production box costs a day. Opening one
    against somebody's live fleet costs their trust, and trust is what makes the
    next twenty pull requests get read.
    """
    ctx = classify(account_name="acme-sandbox", namespace="prod")
    assert ctx.kind == "nonprod", ctx.reason()


def test_a_load_test_that_mentions_prod_is_still_a_load_test():
    """"prod-loadtest" is the exact case that started this."""
    assert classify(resource_name="prod-loadtest-cluster").kind == "ephemeral"


def test_ephemeral_environments_are_recognised():
    for name in ("pr-4821-preview", "review-app-19", "feature-checkout-x"):
        assert classify(namespace=name).kind == "ephemeral", name


def test_tag_keys_are_matched_however_the_team_cased_them():
    for key in ("Environment", "environment", "ENV", "env", "Stage", "deployment-environment"):
        assert classify(tags={key: "staging"}).is_nonprod, key


def test_an_unrelated_tag_is_not_read_as_an_environment():
    """A team called "sandbox-squad" owning production is not a sandbox."""
    assert classify(tags={"Team": "sandbox-squad", "Owner": "dev-platform"}).kind == "unknown"


# ── the sentence a person reads ──────────────────────────────────────────────

def test_the_suppression_note_names_the_evidence_not_just_the_verdict():
    """"nable skipped this" with no reason is indistinguishable from a bug.

    The reader has to be able to disagree, which means seeing the string that
    made the decision and the way to override it.
    """
    note = suppression_note(classify(account_name="acme-sandbox"))

    assert "acme-sandbox" in note, "the note must quote what it matched on"
    assert "include_nonprod" in note, "the note must say how to proceed anyway"


# ── the call site, because a helper nobody calls does nothing ────────────────

def _row(**kw):
    """Minimal stand-in for a savings_recommendations row."""
    base = dict(id=1, resource_id="i-0abc", resource_name="api-7", account_id="4815",
                current_config="{}", recommended_config='{"instance_type":"m5.large"}')
    base.update(kw)
    return type("Row", (), base)()


def _skips(row, include_nonprod=False):
    """Run only the suppression decision the PR opener makes, on one row.

    Mirrors the branch in open_rightsizing_pr rather than re-implementing the
    classifier: the point is to prove the call site consults it.
    """
    from finops.context.workload import classify as _classify
    if include_nonprod:
        return False
    ctx = _classify(
        tags=json.loads(row.current_config or "{}").get("tags") or {},
        account_name=row.account_id,
        resource_name=row.resource_name,
    )
    return ctx.is_nonprod


def test_the_pr_opener_actually_consults_the_classifier():
    """Mutating the call site must break this, not only mutating the helper.

    The recurring defect in this codebase is a correct helper that nothing calls.
    This asserts the wiring exists in the source of open_rightsizing_pr, then
    exercises the decision it makes.
    """
    import ast
    import inspect
    import textwrap

    from finops.remediation import rightsizing_pr

    # Read the structure, not the text. A first pass here asserted the strings
    # "include_nonprod" and "classify(" appeared somewhere in the function, and
    # a mutation replacing the guard with `if False:` left both strings sitting
    # there and sailed straight through. Substring checks pass on dead code.
    tree = ast.parse(textwrap.dedent(inspect.getsource(rightsizing_pr.open_rightsizing_pr)))

    def names(node):
        return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}

    def calls(node):
        return {n.func.id for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}

    guards = [n for n in ast.walk(tree)
              if isinstance(n, ast.If) and "include_nonprod" in names(n.test)]
    assert guards, (
        "no branch in open_rightsizing_pr tests include_nonprod, so every "
        "recommendation now gets a pull request regardless of workload")

    guard = guards[0]
    body_calls = set().union(*(calls(stmt) for stmt in guard.body)) if guard.body else set()
    assert "classify" in body_calls, (
        "the include_nonprod branch no longer calls classify, so the flag is "
        "advertised and the classification never happens")
    assert any(isinstance(n, ast.Continue) for stmt in guard.body for n in ast.walk(stmt)), (
        "the branch classifies but never skips the recommendation, so a sandbox "
        "resource is identified and then patched anyway")

    sig = inspect.signature(rightsizing_pr.open_rightsizing_pr)
    assert sig.parameters["include_nonprod"].default is False, (
        "suppression has to be the default; an opt-in safety feature protects "
        "only the people who already knew to ask for it")


def test_a_sandbox_recommendation_is_held_back():
    assert _skips(_row(account_id="acme-sandbox")) is True
    assert _skips(_row(current_config='{"tags":{"Environment":"staging"}}')) is True


def test_a_production_recommendation_still_gets_its_pull_request():
    assert _skips(_row(current_config='{"tags":{"Environment":"production"}}')) is False


def test_an_untagged_recommendation_still_gets_its_pull_request():
    """The regression that would matter most, stated as its own test."""
    assert _skips(_row()) is False


def test_the_override_reaches_the_decision():
    """A held-back recommendation has to be obtainable, or the note lies."""
    assert _skips(_row(account_id="acme-sandbox"), include_nonprod=True) is False


# ── the signals have to carry data, not just exist ──────────────────────────

def test_the_tag_signal_is_actually_populated_by_the_scan():
    """The classifier's strongest signal was always empty.

    classify() weights an Environment tag above every other signal, and the
    rightsizing recommendation never carried tags: describe_instances returned
    them and _list_instances kept only Name, one line after they arrived. So the
    guard that keeps pull requests off somebody's sandbox ran on the two weakest
    signals it has, and my own mutation tests passed because they exercised the
    helper and the call site's structure, never whether the data reaching it was
    non-empty.

    This asserts the wire, end to end, at the two points where it used to break.
    """
    import pathlib as _p

    # Read from disk, not via import: importing finops.tools.aws_waste here
    # triggers a circular import, and a test that cannot run proves nothing.
    root = _p.Path(__file__).resolve().parents[1] / "src/finops"
    src = (root / "recommendations/rightsizing.py").read_text()
    assert '"tags": {t["Key"]: t["Value"] for t in inst.get("Tags", [])}' in src, (
        "_list_instances dropped the tags again, so Environment never reaches "
        "the classifier")
    assert "tags: dict" in src, "the recommendation cannot carry what it has no field for"
    assert "tags=inst.get(\"tags\") or {}" in src, (
        "the recommendation is built without the tags the scan collected")

    recorded = (root / "tools/aws_waste.py").read_text()
    assert '"tags": getattr(rec, "tags", {}) or {}' in recorded, (
        "the tags die at the DB boundary, which is where the classifier reads them")


def test_the_classifier_reads_the_tags_the_recommendation_stores():
    """The two halves have to agree on where the tags live.

    A scan that writes current_config["tags"] and a call site that reads
    current_config["labels"] would both look right in isolation and pass every
    test either of them owns.
    """
    import inspect

    from finops.remediation import rightsizing_pr

    src = inspect.getsource(rightsizing_pr.open_rightsizing_pr)
    assert '_cfg.get("tags")' in src, (
        "the call site no longer reads the key the scan writes")
    assert "account_name=" not in src, (
        "account_name is back, and there is still no column to populate it from")


def test_a_production_tag_survives_the_round_trip():
    """The shape the wire produces, run through the classifier for real."""
    import json

    stored = json.dumps({"instance_type": "m5.large",
                         "tags": {"Environment": "production", "Name": "api-7"}})
    ctx = classify(tags=json.loads(stored)["tags"])
    assert ctx.kind == "prod" and not ctx.is_nonprod


def test_a_sandbox_tag_survives_the_round_trip():
    import json

    stored = json.dumps({"instance_type": "m5.large",
                         "tags": {"Environment": "sandbox"}})
    ctx = classify(tags=json.loads(stored)["tags"])
    assert ctx.is_nonprod, "a tagged sandbox still reaches a pull request"
