"""Every deep-audit finding must carry an evidence classification.

The load-bearing test is `test_every_emitted_waste_type_is_classified`. `spec_for`
falls back to INFERRED for an unknown type, which is the safe runtime behaviour but
would also let a new detector quietly ship as a permanent low-confidence
investigation. This test is what makes the map self-healing: add a detector, fail
here, classify it.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from finops.analyzers import waste_evidence as we
from finops.recommendations.envelope import INFERRED, MEASURED

_SRC = Path(we.__file__).resolve().parent


def _emitted_waste_types() -> set[str]:
    """Scrape the waste_type literals the detectors actually emit."""
    found: set[str] = set()
    for name in ("waste.py", "optimizer.py"):
        text = (_SRC / name).read_text()
        found |= set(re.findall(r'"waste_type":\s*"([a-z0-9_]+)"', text))
    return found


def test_every_emitted_waste_type_is_classified():
    emitted = _emitted_waste_types()
    assert emitted, "scraper found no waste_type literals; the regex has rotted"
    unclassified = sorted(emitted - set(we.WASTE_EVIDENCE))
    assert not unclassified, (
        "these detectors emit findings with no evidence classification, so they would "
        f"ship as unexplained low-confidence investigations: {unclassified}. "
        "Add them to WASTE_EVIDENCE in analyzers/waste_evidence.py."
    )


def test_no_stale_entries_in_the_map():
    """A classification for a detector that no longer exists is dead weight and
    hides the fact that coverage dropped."""
    stale = sorted(set(we.WASTE_EVIDENCE) - _emitted_waste_types())
    assert not stale, f"WASTE_EVIDENCE classifies types nothing emits: {stale}"


def test_every_inferred_type_explains_itself_and_offers_a_next_step():
    """An investigation with no why_unsure is just a recommendation we lost our
    nerve on. The whole point is telling the user what we do not know and how to
    close the gap."""
    for wt, spec in we.WASTE_EVIDENCE.items():
        if spec.evidence != INFERRED:
            continue
        assert spec.why_unsure.strip(), f"{wt}: inferred with no why_unsure"
        assert spec.confirm_steps, f"{wt}: inferred with no confirm_steps"


def test_evidence_values_are_valid():
    for wt, spec in we.WASTE_EVIDENCE.items():
        assert spec.evidence in (MEASURED, INFERRED), f"{wt}: bad evidence {spec.evidence!r}"
        assert spec.confidence in ("high", "medium", "low"), f"{wt}: bad confidence"


# ── the invariant that makes the split honest ───────────────────────────────


def test_annotate_marks_measured_as_recommendation():
    findings = [{"waste_type": "unattached_ebs_volume", "estimated_monthly_savings": 42.0}]
    out = we.annotate(findings)
    assert out[0]["evidence"] == MEASURED
    assert out[0]["kind"] == "recommendation"
    assert out[0]["estimated_monthly_savings"] == 42.0
    assert "magnitude" not in out[0], "a recommendation states the figure, not a band"


def test_annotate_downgrades_cpu_only_idle_ec2_to_an_investigation():
    """The single most-shown finding is CPU-only and is the classic false positive.
    If this ever classifies as a recommendation, we are promising something we did
    not measure."""
    findings = [{"waste_type": "idle_ec2_low_cpu", "estimated_monthly_savings": 900.0}]
    out = we.annotate(findings)
    assert out[0]["kind"] == "investigation"
    assert out[0]["magnitude"] == "~hundreds/mo"
    assert "memory" in out[0]["why_unsure"].lower()
    assert out[0]["confirm_steps"]
    # Raw figure survives for ranking and the ledger; only the CLAIM changes.
    assert out[0]["estimated_monthly_savings"] == 900.0


def test_unknown_waste_type_fails_safe_to_investigation():
    out = we.annotate([{"waste_type": "something_new", "estimated_monthly_savings": 10.0}])
    assert out[0]["kind"] == "investigation", "an unclassified finding must not be a recommendation"


def test_annotate_drops_empty_fields():
    """Findings ship to the model in lists of dozens; empty keys are pure token cost."""
    out = we.annotate([{"waste_type": "unassociated_elastic_ip", "estimated_monthly_savings": 3.6}])
    assert "why_unsure" not in out[0]
    assert "confirm_steps" not in out[0]


def test_split_totals_separates_measured_from_unconfirmed():
    findings = we.annotate([
        {"waste_type": "unattached_ebs_volume", "estimated_monthly_savings": 100.0},
        {"waste_type": "unassociated_elastic_ip", "estimated_monthly_savings": 3.6},
        {"waste_type": "idle_ec2_low_cpu", "estimated_monthly_savings": 900.0},
        {"waste_type": "s3_suboptimal_storage_class", "estimated_monthly_savings": 50.0},
    ])
    totals = we.split_totals(findings)
    assert totals["measured_monthly_savings"] == 103.6
    assert totals["measured_count"] == 2
    assert totals["unconfirmed_monthly_opportunity"] == 950.0
    assert totals["unconfirmed_count"] == 2
    # The two must not be silently added together anywhere in this dict.
    assert "total" not in " ".join(totals).lower()


def test_split_totals_handles_missing_and_bad_savings():
    findings = we.annotate([
        {"waste_type": "unattached_ebs_volume"},                                 # no figure
        {"waste_type": "idle_ec2_low_cpu", "estimated_monthly_savings": None},   # explicit None
    ])
    totals = we.split_totals(findings)
    assert totals["measured_monthly_savings"] == 0.0
    assert totals["unconfirmed_monthly_opportunity"] == 0.0
    assert totals["unconfirmed_band"] == "under ~$100/mo"


def test_split_totals_on_empty_input():
    totals = we.split_totals([])
    assert totals["measured_monthly_savings"] == 0.0
    assert totals["unconfirmed_band"] is None
