"""The critique pass: try to refute a recommendation before a human sees it.

The commercial stake is narrow. The first recommendation a new install shows is the
whole trust relationship, and the cheapest way to lose it is a confident dollar
figure that assumed a full month on an instance launched last Tuesday. These tests
pin the arithmetic traps, the propose-only guarantee (nothing is ever deleted, no
cloud call is ever made), and the fact that a blocked claim degrades to an
investigation rather than vanishing.

The LLM layer is exercised through an injected fake. It is opt-in and off by
default, and a test asserts that default explicitly: a critic that silently spends
the customer's Anthropic tokens is a bug, not a feature.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from finops.recommendations import critique as C
from finops.recommendations.envelope import INVESTIGATION, RECOMMENDATION

TODAY = date(2026, 8, 15)


def _rec(**kw):
    base = {
        "source": "rightsizing",
        "title": "Downsize db.r5.xlarge",
        "resource_id": "i-0abc123",
        "region": "us-east-1",
        "estimated_monthly_savings_usd": 1200.0,
        "current_monthly_cost_usd": 3000.0,
        "lookback_days": 30,
    }
    base.update(kw)
    return base


def _codes(objs):
    return {o.code for o in objs}


# ── the arithmetic traps ──────────────────────────────────────────────────────

def test_full_month_claimed_on_a_resource_days_old():
    rec = _rec(launch_time=(TODAY - timedelta(days=3)).isoformat())
    objs = C.falsifiers(rec, today=TODAY)
    assert "full_month_on_new_resource" in _codes(objs)
    o = next(o for o in objs if o.code == "full_month_on_new_resource")
    assert o.blocking, "3 days old with a full-month claim has to block"
    assert "3 days old" in o.detail


def test_a_resource_three_weeks_old_is_a_caveat_not_a_block():
    # Old enough that a monthly rate is a defensible extrapolation. Say it, don't
    # retract the number: over-blocking trains people to ignore the critique.
    rec = _rec(launch_time=(TODAY - timedelta(days=21)).isoformat())
    objs = C.falsifiers(rec, today=TODAY)
    o = next(o for o in objs if o.code == "full_month_on_new_resource")
    assert not o.blocking


def test_mature_resource_raises_nothing():
    rec = _rec(launch_time=(TODAY - timedelta(days=400)).isoformat())
    assert C.falsifiers(rec, today=TODAY) == []


def test_savings_cannot_exceed_what_the_resource_costs():
    rec = _rec(estimated_monthly_savings_usd=5000.0, current_monthly_cost_usd=3000.0)
    objs = C.falsifiers(rec, today=TODAY)
    assert "savings_exceed_resource_spend" in _codes(objs)
    assert all(o.blocking for o in objs if o.code == "savings_exceed_resource_spend")


def test_commitment_argued_from_nine_days_is_blocked():
    rec = _rec(source="commitments", title="Buy a 1-year Compute Savings Plan",
               lookback_days=9)
    objs = C.falsifiers(rec, today=TODAY)
    assert "commitment_outlives_evidence" in _codes(objs)
    assert next(o for o in objs if o.code == "commitment_outlives_evidence").blocking


def test_short_window_on_a_non_commitment_is_only_a_caveat():
    objs = C.falsifiers(_rec(lookback_days=5), today=TODAY)
    assert "lookback_shorter_than_a_cycle" in _codes(objs)
    assert not any(o.blocking for o in objs)


def test_double_counting_an_existing_commitment_is_blocked():
    objs = C.falsifiers(_rec(covered_by_commitment=True), today=TODAY)
    assert "already_covered_by_commitment" in _codes(objs)
    assert next(o for o in objs if o.code == "already_covered_by_commitment").blocking


def test_commitment_coverage_is_also_read_from_metadata():
    # Scanners disagree on whether this lives top-level or in metadata. A critic
    # that only reads one shape reports a clean bill of health it never earned.
    objs = C.falsifiers(_rec(metadata={"ri_covered": True}), today=TODAY)
    assert "already_covered_by_commitment" in _codes(objs)


def test_price_comparison_against_another_region_is_blocked():
    objs = C.falsifiers(_rec(region="eu-west-1", comparison_region="us-east-1"), today=TODAY)
    assert "peer_region_mismatch" in _codes(objs)


def test_a_zero_or_negative_saving_is_not_a_recommendation():
    assert "no_positive_saving" in _codes(C.falsifiers(_rec(estimated_monthly_savings_usd=0.0),
                                                       today=TODAY))
    assert "no_positive_saving" in _codes(C.falsifiers(_rec(estimated_monthly_savings_usd=-40.0),
                                                       today=TODAY))


@pytest.mark.parametrize("field", ["launch_time", "launched_at", "created_at"])
def test_age_is_read_from_whichever_field_the_scanner_used(field):
    rec = _rec(**{field: (TODAY - timedelta(days=2)).isoformat()})
    assert "full_month_on_new_resource" in _codes(C.falsifiers(rec, today=TODAY))


def test_unparseable_or_absent_dates_never_crash():
    assert C.falsifiers(_rec(launch_time="not a date"), today=TODAY) == []
    assert C.falsifiers(_rec(launch_time=None), today=TODAY) == []


# ── the pass, and what a blocked claim becomes ────────────────────────────────

def test_blocked_recommendation_degrades_to_an_investigation_and_loses_the_number():
    rec = _rec(estimated_monthly_savings_usd=5000.0, current_monthly_cost_usd=3000.0)
    out = C.critique([rec], use_llm=False, today=TODAY)[0]

    assert out["kind"] == INVESTIGATION
    assert out["estimated_monthly_savings_usd"] is None   # no false precision survives
    assert out["magnitude"] == "~thousands/mo"            # the signal does
    assert out["why_unsure"]
    assert out["confirm_steps"]
    assert out["critique"]["survived"] is False


def test_a_surviving_recommendation_keeps_its_number():
    out = C.critique([_rec()], use_llm=False, today=TODAY)[0]
    assert out["kind"] == RECOMMENDATION
    assert out["estimated_monthly_savings_usd"] == 1200.0
    assert out["critique"]["survived"] is True
    assert out["critique"]["objection_count"] == 0


def test_critique_is_propose_only_and_never_drops_anything():
    recs = [_rec(resource_id="i-1"),
            _rec(resource_id="i-2", estimated_monthly_savings_usd=9999.0,
                 current_monthly_cost_usd=100.0),
            _rec(resource_id="i-3")]
    out = C.critique(recs, use_llm=False, today=TODAY)
    assert len(out) == 3
    assert [r["resource_id"] for r in out] == ["i-1", "i-2", "i-3"]


def test_the_callers_recommendations_are_never_mutated():
    rec = _rec(estimated_monthly_savings_usd=5000.0, current_monthly_cost_usd=3000.0)
    C.critique([rec], use_llm=False, today=TODAY)
    assert rec["estimated_monthly_savings_usd"] == 5000.0
    assert "critique" not in rec


def test_critique_imports_nothing_that_can_touch_a_cloud():
    # Same guarantee rescorer carries. The critic runs over every finding in an
    # audit; if it could call a cloud API, a read-only pass would stop being one.
    import inspect

    src = inspect.getsource(C)
    for banned in ("import boto3", "from boto3", "google.cloud", "azure."):
        assert banned not in src, f"critique.py must not import {banned}"


# ── the LLM layer ─────────────────────────────────────────────────────────────

def test_llm_is_off_unless_explicitly_opted_in(monkeypatch):
    # Spending the customer's Anthropic tokens without being asked is a bug.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("NABLE_CRITIC_LLM", raising=False)
    assert C.llm_enabled() is False

    monkeypatch.setenv("NABLE_CRITIC_LLM", "1")
    assert C.llm_enabled() is True

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert C.llm_enabled() is False, "opt-in without a key must not claim to be enabled"


def test_llm_objection_can_block_a_claim_the_rules_would_have_passed(monkeypatch):
    # The whole point of the layer: a month-end batch job is invisible to every
    # deterministic check and fatal to the recommendation.
    monkeypatch.setattr(C, "_llm_objections", lambda rec, context="": [
        C.Objection(code="periodic_spike_hidden_by_average", blocking=True, source="llm",
                    detail="idle on a 30-day mean because it runs month-end close at 90% for four hours."),
    ])
    out = C.critique([_rec(estimated_monthly_savings_usd=1200.0)], use_llm=True, today=TODAY)[0]
    assert out["kind"] == INVESTIGATION
    assert out["critique"]["objections"][0]["source"] == "llm"


def test_small_claims_never_reach_the_model(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(C, "_llm_objections", lambda rec, context="": calls.append(rec) or [])
    C.critique([_rec(estimated_monthly_savings_usd=40.0)], use_llm=True,
               llm_floor_usd=500.0, today=TODAY)
    assert calls == [], "a $40 claim is not worth a token"


def test_an_already_blocked_claim_never_reaches_the_model(monkeypatch):
    # It is going to be downgraded either way; a second opinion changes nothing
    # and still costs money.
    calls: list[dict] = []
    monkeypatch.setattr(C, "_llm_objections", lambda rec, context="": calls.append(rec) or [])
    C.critique([_rec(estimated_monthly_savings_usd=9000.0, current_monthly_cost_usd=1000.0)],
               use_llm=True, llm_floor_usd=500.0, today=TODAY)
    assert calls == []


def test_the_model_is_reached_for_a_large_clean_claim(monkeypatch):
    # The positive control for the two skip tests above: without it, they would
    # both pass on a critique() that never calls the model at all.
    calls: list[dict] = []
    monkeypatch.setattr(C, "_llm_objections", lambda rec, context="": calls.append(rec) or [])
    C.critique([_rec(estimated_monthly_savings_usd=9000.0, current_monthly_cost_usd=20000.0)],
               use_llm=True, llm_floor_usd=500.0, today=TODAY)
    assert len(calls) == 1


def test_the_real_llm_helper_swallows_its_own_failures(monkeypatch):
    # _llm_objections owns the try/except, so an outage degrades to "no objections"
    # and the audit still ships.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(C, "_prompt_payload", lambda rec: (_ for _ in ()).throw(RuntimeError("boom")))
    assert C._llm_objections(_rec()) == []


def test_the_prompt_carries_no_credentials(monkeypatch):
    # An MCP tool must never route a secret through a model provider. The critic
    # sends a rec dict it did not build, so pin what it forwards.
    # The placeholders below are literal decoys, not credentials. detect-secrets
    # flags the `api_key=` keyword, which is the scanner doing its job on a test
    # whose whole purpose is proving that exact leak cannot happen.
    body = C._prompt_payload(_rec(
        aws_secret_access_key="SHOULD-NEVER-APPEAR",  # pragma: allowlist secret
        api_key="SHOULD-NEVER-APPEAR",  # pragma: allowlist secret
        # metadata rides to the model as the one open-ended field, so a secret
        # nested one level down is the likeliest future leak, not a top-level key.
        metadata={"api_token": "SHOULD-NEVER-APPEAR",  # pragma: allowlist secret
                  "session_key": "SHOULD-NEVER-APPEAR",  # pragma: allowlist secret
                  "nested": {"anything": "SHOULD-NEVER-APPEAR"},
                  "kms_key_id": "arn:aws:kms:us-east-1:123456789012:key/abc",
                  "estimated_monthly_kms_calls": 4200},
    ))
    assert "SHOULD-NEVER-APPEAR" not in body
    assert "i-0abc123" in body            # resource identifiers are the point
    assert "kms_key_id" in body           # an identifier with "key" in its name survives
    assert "4200" in body                 # and so do plain metrics


def test_sensitive_key_detection_is_by_class_not_spelling():
    for name in ("API_KEY", "x-authorization", "dbPassword", "refresh_token",
                 "AWS_SECRET_ACCESS_KEY", "cookie"):
        assert C._key_is_sensitive(name), name
    for name in ("kms_key_id", "instance_id", "topic_arn", "key_alias", "region"):
        assert not C._key_is_sensitive(name), name


# ── the age the AWS analyzers actually emit ──────────────────────────────────

def test_a_numeric_age_days_fires_the_new_resource_falsifier():
    """The gap that made the headline check inert where it mattered most.

    analyzers/waste.py emits `age_days` as a number and carries no date string
    at all, so _age_days returned None for every AWS waste finding and a
    $5,000/mo claim on a three-day-old volume passed review untouched. Found by
    building a finding shaped like a real one instead of like a test fixture.
    """
    from datetime import date

    from finops.recommendations.critique import _age_days, critique

    today = date(2026, 8, 3)
    for placement in ({"age_days": 3}, {"metadata": {"age_days": 3}}):
        rec = {"resource_id": "vol-new",
               "estimated_monthly_savings_usd": 5000.0, **placement}
        assert _age_days(rec, today=today) == 3.0
        out = critique([rec], today=today, use_llm=False)[0]
        assert out["critique"]["survived"] is False, placement
        assert out["estimated_monthly_savings_usd"] is None


def test_idle_days_is_not_treated_as_resource_age():
    """A volume idle 74 days can be three years old. Conflating the two would
    retract correct findings, which is worse than missing some."""
    from datetime import date

    from finops.recommendations.critique import _age_days, critique

    rec = {"resource_id": "vol-old", "estimated_monthly_savings_usd": 5000.0,
           "idle_days": 3}
    assert _age_days(rec, today=date(2026, 8, 3)) is None
    assert critique([rec], today=date(2026, 8, 3), use_llm=False)[0]["critique"]["survived"]


def test_an_old_resource_still_survives_and_garbage_ages_are_ignored():
    from datetime import date

    from finops.recommendations.critique import _age_days, critique

    today = date(2026, 8, 3)
    assert _age_days({"age_days": 400}, today=today) == 400.0
    for junk in ("n/a", True, None, float("nan"), -5, [1]):
        assert _age_days({"age_days": junk}, today=today) is None, junk
    survived = critique([{"resource_id": "v", "estimated_monthly_savings_usd": 5000.0,
                          "age_days": 400}], today=today, use_llm=False)[0]
    assert survived["critique"]["survived"] is True
